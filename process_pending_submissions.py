#!/usr/bin/env python

import argparse
import json
import os
import sys
import urllib

import django
from django.conf import settings
from django.utils import timezone

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'settings')
sys.path.insert(0, os.getcwd())
django.setup()

import point_source_extraction
import imageflow.s3_util as s3_util
from astrometry.models import AstrometrySubmission, AstrometrySubmissionJob
from astrometry.astrometry_client import Client

class SubmissionHandler():

    def __init__(self, client, submission, args):
        self.client = client
        self.submission = submission
        self.args = args

    def handle_pending_submission(self):
        client = self.client
        submission = self.submission

        print 'Querying for submission %d...' % (submission.subid)
        substatus = client.sub_status(submission.subid, True)

        if not (substatus and 'processing_finished' in substatus):
            print 'Submission is not done submitting yet.'
            return False

        job_ids = substatus['jobs']
        print 'Submission has processing jobs: %s' % (job_ids)
        num_success = 0
        for job_id in job_ids:
            info = client.send_request('jobs/%d/info' % (job_id))

            status = info['status']
            if status == 'solving' or status == 'processing':
                print '-> Job %d is still solving' % (job_id)
                # TODO(ian): Sometimes an image can get stuck in this state; there
                # should be a maximum timeout.
                return False
            elif status == 'failure':
                print '-> Job %d has failed' % (job_id)
                submission.status = AstrometrySubmission.FAILED_TO_PROCESS
                submission.save()
                return False
            elif status != 'success':
                print '-> Warning: unknown status %s: job %d, submission %d' \
                        % (status, job_id, submission.subid)
                print '-> Got the following response:', info
                return False

            annotations = client.send_request('jobs/%d/annotations' % (job_id))

            # Save these results.
            job = AstrometrySubmissionJob.objects.create(
                    submission=submission,
                    jobid=job_id,
                    status=AstrometrySubmissionJob.SUCCESS,
                    annotations=annotations,
                    info=info)
            num_success += 1
            print '-> Job %d was added' % (job_id)

        if num_success > 0 and num_success == len(job_ids):
            self.process_completed_submission(job)
            return True
        return False

    def process_completed_submission(self, job):
        submission = self.submission

        print '-> Submission %d, Job %d is complete' % (submission.subid, job.jobid)

        # Save results.
        self.save_submission_results(job)

        # Update submission.
        submission.succeeded_at = timezone.now()
        submission.status = AstrometrySubmission.COMPLETE
        if not args.dry_run:
            submission.save()

    def save_submission_results(self, job):
        submission = self.submission

        print '-> Uploading results for submission %d' % (submission.subid)

        annotated_display_url = 'http://nova.astrometry.net/annotated_display/%d' \
                % (job.jobid)
        new_image_fits_url = 'http://nova.astrometry.net/new_fits_file/%d' \
                % (job.jobid)
        corr_url = 'http://nova.astrometry.net/corr_file/%d' \
                % (job.jobid)

        # Timestamp is added to name automatically.
        upload_key_prefix = 'processed/%d' % (submission.subid)

        # Annotated jpg.
        name = '%d_%d_annotated.jpg' % (submission.subid, job.jobid)
        print '  -> Uploading', name, '...'
        if not args.dry_run:
            s3_util.upload_to_s3_via_url(annotated_display_url, upload_key_prefix, name)

        # CORR.
        name = '%d_%d_corr.fits' % (submission.subid, job.jobid)
        print '  -> Uploading', name, '...'
        if not args.dry_run:
            s3_util.upload_to_s3_via_url(corr_url, upload_key_prefix, name)

        # FITS.
        name = '%d_%d_image.fits' % (submission.subid, job.jobid)
        fits_image_data = urllib.urlopen(new_image_fits_url).read()
        print '  -> Uploading', name, '...'
        if not args.dry_run:
            s3_util.upload_to_s3(fits_image_data, upload_key_prefix, name)

        print '-> Uploaded results for submission %d' % (submission.subid)

        # Point source extraction processing.
        self.process_fits_image(fits_image_data, job, upload_key_prefix)

    def process_fits_image(self, image_data, job, upload_key_prefix):
        submission = self.submission

        print '-> Processing fits image for submission %d' % (submission.subid)

        data = point_source_extraction.load_data_as_fits(image_data)
        sources = point_source_extraction.compute(data)

        # Coords.
        coords_plot_path = '%d_%d_plot.png' % (submission.subid, job.jobid)
        point_source_extraction.plot(sources, data, coords_plot_path)
        print '  -> Uploading', coords_plot_path, '...'
        if not args.dry_run:
            s3_util.upload_to_s3_via_file(coords_plot_path, upload_key_prefix)

        coords_fits_path = '%d_%d_coords.fits' % (submission.subid, job.jobid)
        point_source_extraction.save_fits(sources, coords_fits_path)
        print '  -> Uploading', coords_fits_path, '...'
        if not args.dry_run:
            s3_util.upload_to_s3_via_file(coords_fits_path, upload_key_prefix)

        coords_json_path = '%d_%d_coords.json' % (submission.subid, job.jobid)
        point_source_extraction.save_json(sources, coords_json_path)
        print '  -> Uploading', coords_json_path, '...'
        if not args.dry_run:
            s3_util.upload_to_s3_via_file(coords_json_path, upload_key_prefix)

        # PSF.
        psf_scatter_path = '%d_%d_psf_scatter.png' % (submission.subid, job.jobid)
        psf_bar_path = '%d_%d_psf_bar.png' % (submission.subid, job.jobid)
        psf_residual_path = '%d_%d_psf_residual.png' % (submission.subid, job.jobid)
        point_source_extraction.compute_psf_flux(data, sources, \
                psf_scatter_path, psf_bar_path, psf_residual_path)

        print '  -> Uploading', psf_scatter_path, '...'
        print '  -> Uploading', psf_bar_path, '...'
        print '  -> Uploading', psf_residual_path, '...'
        if not args.dry_run:
            s3_util.upload_to_s3_via_file(psf_scatter_path, upload_key_prefix)
            s3_util.upload_to_s3_via_file(psf_bar_path, upload_key_prefix)
            s3_util.upload_to_s3_via_file(psf_residual_path, upload_key_prefix)

        # TODO(ian): Should delete the files afterwards, or create them as
        # temporary files.

        print '-> Processed fits image for submission %d' % (submission.subid)

def get_args():
    parser = argparse.ArgumentParser('Process outstanding jobs')
    parser.add_argument('--dry_run', help='don\'t actually do anything', action='store_true')
    return parser.parse_args()

if __name__ == '__main__':
    args = get_args()

    # Set up astrometry.net client.
    client = Client()
    client.login(settings.ASTROKIT_ASTROMETRY_KEY)

    pending_submissions = AstrometrySubmission.objects.all().filter(
            status=AstrometrySubmission.SUBMITTED)
    for submission in pending_submissions:
        handler = SubmissionHandler(client, submission, args)
        handler.handle_pending_submission()
