#!/usr/bin/env python

import argparse
import json
import logging
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
from imageflow.models import AnalysisResult

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class SubmissionHandler():
    def __init__(self, client, submission, args):
        self.client = client
        self.submission = submission
        self.args = args

    def run(self):
        self.handle_pending_submission()

    def handle_pending_submission(self):
        client = self.client
        submission = self.submission

        logger.info('Querying for submission %d...' % (submission.subid))
        substatus = client.sub_status(submission.subid, True)

        if not (substatus and 'processing_finished' in substatus):
            logger.info('Submission is not done submitting yet.')
            return False

        job_ids = substatus['jobs']
        logger.info('Submission has processing jobs: %s' % (job_ids))
        num_success = 0
        for job_id in job_ids:
            info = client.send_request('jobs/%d/info' % (job_id))

            status = info['status']
            if status == 'solving' or status == 'processing':
                logger.info('-> Job %d is still solving' % (job_id))
                # TODO(ian): Sometimes an image can get stuck in this state; there
                # should be a maximum timeout.
                return False
            elif status == 'failure':
                logger.warn('-> Job %d has failed' % (job_id))
                submission.status = AstrometrySubmission.FAILED_TO_PROCESS
                submission.save()
                return False
            elif status != 'success':
                logger.warn('-> Warning: unknown status %s: job %d, submission %d' \
                        % (status, job_id, submission.subid))
                logger.warn('-> Got the following response: %s' % info)
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
            logger.info('-> Job %d was added' % (job_id))

        if num_success > 0 and num_success == len(job_ids):
            self.process_completed_submission(job)
            return True
        return False

    def process_completed_submission(self, job):
        submission = self.submission
        result = AnalysisResult.objects.create(astrometry_job=job)

        logger.info('-> Submission %d, Job %d is complete' % (submission.subid, job.jobid))

        # Save results.
        self.save_submission_results(job, result)

        # Update submission.
        submission.succeeded_at = timezone.now()
        submission.status = AstrometrySubmission.COMPLETE
        if not args['dry_run']:
            submission.save()

        result.status = AnalysisResult.COMPLETE
        result.save()

    def save_submission_results(self, job, result):
        submission = self.submission

        logger.info('-> Uploading results for submission %d' % (submission.subid))

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
        logger.info('  -> Uploading %s...' % name)
        if not args['dry_run']:
            result.astrometry_annotated_display_url = \
                    s3_util.upload_to_s3_via_url(annotated_display_url, \
                                                 upload_key_prefix, name)

        # CORR.
        name = '%d_%d_corr.fits' % (submission.subid, job.jobid)
        logger.info('  -> Uploading %s...' % name)
        if not args['dry_run']:
            result.astrometry_corr_fits_url = \
                    s3_util.upload_to_s3_via_url(corr_url, \
                                                 upload_key_prefix, name)

        # FITS.
        name = '%d_%d_image.fits' % (submission.subid, job.jobid)
        fits_image_data = urllib.urlopen(new_image_fits_url).read()
        logger.info('  -> Uploading %s...' % name)
        if not args['dry_run']:
            result.astrometry_image_fits_url = \
                    s3_util.upload_to_s3(fits_image_data, \
                                         upload_key_prefix, name)

        logger.info('-> Uploaded results for submission %d' % (submission.subid))

        # Point source extraction processing.
        self.process_fits_image(fits_image_data, job, result, upload_key_prefix)

    def process_fits_image(self, image_data, job, result, upload_key_prefix):
        submission = self.submission

        logger.info('-> Processing fits image for submission %d' % (submission.subid))

        data = point_source_extraction.load_data_as_fits(image_data)
        sources = point_source_extraction.compute(data)

        # Coords.
        coords_plot_path = '%d_%d_plot.png' % (submission.subid, job.jobid)
        point_source_extraction.plot(sources, data, coords_plot_path)
        logger.info('  -> Uploading %s...' % coords_plot_path)
        if not args['dry_run']:
            result.coords_plot_url = \
                    s3_util.upload_to_s3_via_file(coords_plot_path, \
                                                  upload_key_prefix)

        coords_fits_path = '%d_%d_coords.fits' % (submission.subid, job.jobid)
        point_source_extraction.save_fits(sources, coords_fits_path)
        logger.info('  -> Uploading %s...' % coords_fits_path)
        if not args['dry_run']:
            result.coords_fits_url = \
                    s3_util.upload_to_s3_via_file(coords_fits_path, \
                                                  upload_key_prefix)

        coords_json_path = '%d_%d_coords.json' % (submission.subid, job.jobid)
        point_source_extraction.save_json(sources, coords_json_path)
        logger.info('  -> Uploading %s...' % coords_json_path)
        if not args['dry_run']:
            result.coords_json_url = \
                    s3_util.upload_to_s3_via_file(coords_json_path, \
                                                  upload_key_prefix)

        # PSF.
        psf_scatter_path = '%d_%d_psf_scatter.png' % (submission.subid, job.jobid)
        psf_bar_path = '%d_%d_psf_bar.png' % (submission.subid, job.jobid)
        psf_hist_path = '%d_%d_psf_hist.png' % (submission.subid, job.jobid)
        psf_residual_path = '%d_%d_psf_residual.png' % (submission.subid, job.jobid)
        point_source_extraction.compute_psf_flux(data, sources, \
                psf_scatter_path, psf_bar_path, psf_hist_path, psf_residual_path)

        logger.info('  -> Uploading %s' % psf_scatter_path)
        logger.info('  -> Uploading %s' % psf_bar_path)
        logger.info('  -> Uploading %s' % psf_hist_path)
        logger.info('  -> Uploading %s' % psf_residual_path)
        if not args['dry_run']:
            result.psf_scatter_url = \
                    s3_util.upload_to_s3_via_file(psf_scatter_path, \
                                                  upload_key_prefix)
            result.psf_bar_url = \
                    s3_util.upload_to_s3_via_file(psf_bar_path, \
                                                  upload_key_prefix)
            result.psf_hist_url = \
                    s3_util.upload_to_s3_via_file(psf_hist_path, \
                                                  upload_key_prefix)
            result.psf_residual_image_url = \
                    s3_util.upload_to_s3_via_file(psf_residual_path, \
                                                  upload_key_prefix)

        # TODO(ian): Should delete the files afterwards, or create them as
        # temporary files.

        logger.info('-> Processed fits image for submission %d' % (submission.subid))

def process_pending_submissions(args):
    # Set up astrometry.net client.
    client = Client()
    client.login(settings.ASTROKIT_ASTROMETRY_KEY)

    pending_submissions = AstrometrySubmission.objects.all().filter(
            status=AstrometrySubmission.SUBMITTED)
    for submission in pending_submissions:
        handler = SubmissionHandler(client, submission, args)
        handler.run()

def get_args():
    parser = argparse.ArgumentParser('Process outstanding jobs')
    parser.add_argument('--dry_run', help='don\'t actually do anything', action='store_true')
    return parser.parse_args()

if __name__ == '__main__':
    args = get_args()
    process_pending_submissions(vars(args))
