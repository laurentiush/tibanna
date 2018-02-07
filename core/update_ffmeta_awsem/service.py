# -*- coding: utf-8 -*-
import logging
from core import utils, ff_utils, ec2_utils
import boto3
from collections import defaultdict
from core.fastqc_utils import parse_qc_table

LOG = logging.getLogger(__name__)
s3 = boto3.resource('s3')


def donothing(status, sbg, ff_meta, ff_key=None):
    return None


def update_processed_file_metadata(status, pf_meta, tibanna):

    ff_key = tibanna.ff_keys
    try:
        for pf in pf_meta:
            pf['status'] = status
    except Exception as e:
        raise Exception("Unable to update processed file metadata json : %s" % e)
    try:
        for pf in pf_meta:
            pfo = ff_utils.ProcessedFileMetadata(**pf)
            pfo.post(key=ff_key)
    except Exception as e:
        raise Exception("Unable to post processed file metadata : %s" % e)
    return pf_meta


def qc_updater(status, wf_file, ff_meta, tibanna):
    if ff_meta.awsem_app_name == 'fastqc-0-11-4-1':
        return _qc_updater(status, wf_file, ff_meta, tibanna,
                           quality_metric='quality_metric_fastqc',
                           file_argument='input_fastq',
                           report_html='fastqc_report.html',
                           datafiles=['summary.txt', 'fastqc_data.txt'])
    elif ff_meta.awsem_app_name == 'pairsqc-single':
        file_argument = 'input_pairs'
        input_accession = str(wf_file.runner.inputfile_accessions[file_argument])
        return _qc_updater(status, wf_file, ff_meta, tibanna,
                           quality_metric="quality_metric_pairsqc",
                           file_argument=file_argument, report_html='pairsqc_report.html',
                           datafiles=[input_accession + '.summary.out'])
    elif ff_meta.awsem_app_name == 'repliseq-parta':
        return _qc_updater(status, wf_file, ff_meta, tibanna,
                           quality_metric='quality_metric_dedupqc_repliseq',
                           file_argument='filtered_sorted_deduped_bam',
                           datafiles=['summary.txt'])


def _qc_updater(status, wf_file, ff_meta, tibanna, quality_metric='quality_metric_fastqc',
                file_argument='input_fastq', report_html=None,
                datafiles=['summary.txt', 'fastqc_data.txt']):
    if status == 'uploading':
        # wait until this bad boy is finished
        return
    # keys
    ff_key = tibanna.ff_keys
    # move files to proper s3 location
    # need to remove sbg from this line
    accession = wf_file.runner.all_file_accessions[file_argument]
    zipped_report = wf_file.key
    files_to_parse = datafiles
    if report_html:
        files_to_parse.append(report_html)
    LOG.info("accession is %s" % accession)

    try:
        files = wf_file.s3.unzip_s3_to_s3(zipped_report, accession, files_to_parse,
                                          acl='public-read')
    except Exception as e:
        LOG.info(tibanna.s3.__dict__)
        raise Exception("%s (key={})\n".format(zipped_report) % e)

    # schema
    qc_schema = ff_utils.get_metadata("profiles/" + quality_metric + ".json", key=ff_key)

    # parse fastqc metadata
    LOG.info("files : %s" % str(files))
    filedata = [files[_]['data'] for _ in datafiles]
    meta = parse_qc_table(filedata,
                          url=files[report_html]['s3key'],
                          qc_schema=qc_schema.get('properties'))
    LOG.info("fastqc meta is %s" % meta)

    # post fastq metadata
    qc_meta = ff_utils.post_to_metadata(meta, quality_metric, key=ff_key)
    if qc_meta.get('@graph'):
        qc_meta = qc_meta['@graph'][0]

    LOG.info("qc_meta is %s" % qc_meta)
    # update original file as well
    try:
        original_file = ff_utils.get_metadata(accession, key=ff_key)
        LOG.info("original_file is %s" % original_file)
    except Exception as e:
        raise Exception("Couldn't get metadata for accession {} : ".format(accession) + str(e))
    patch_file = {'quality_metric': qc_meta['@id']}
    try:
        ff_utils.patch_metadata(patch_file, original_file['uuid'], key=ff_key)
    except Exception as e:
        raise Exception("patch_metadata failed in fastqc_updater." + str(e) +
                        "original_file ={}\n".format(str(original_file)))

    # patch the workflow run, value_qc is used to make drawing graphs easier.
    output_files = ff_meta.output_files
    output_files[0]['value_qc'] = qc_meta['@id']
    retval = {"output_quality_metrics": [{"name": quality_metric, "value": qc_meta['@id']}],
              'output_files': output_files}

    LOG.info("retval is %s" % retval)
    return retval


def md5_updater(status, wf_file, ff_meta, tibanna):
    # get key
    ff_key = tibanna.ff_keys
    # get metadata about original input file
    accession = wf_file.runner.inputfile_accessions['input_file']
    original_file = ff_utils.get_metadata(accession, key=ff_key)

    if status.lower() == 'uploaded':
        md5 = wf_file.read()
        original_md5 = original_file.get('content_md5sum', False)
        current_status = original_file.get('status', "uploading")
        if original_md5 and original_md5 != md5:
            # file status to be upload failed / md5 mismatch
            print("no matcho")
            md5_updater("upload failed", wf_file, ff_meta, tibanna)
        else:
            new_file = {}
            # change status to uploaded only if it is uploading or upload failed
            if current_status in ["uploading", "upload failed"]:
                new_file['status'] = 'uploaded'
            new_file['content_md5sum'] = md5

            try:
                ff_utils.patch_metadata(new_file, accession, key=ff_key)
            except Exception as e:
                # TODO specific excpetion
                # if patch fails try to patch worfklow status as failed
                new_file = {}
                new_file['status'] = 'upload failed'
                new_file['description'] = str(e)
                ff_utils.patch_metadata(new_file, original_file['uuid'], key=ff_key)
    elif status == 'upload failed':
            new_file = {}
            new_file['status'] = 'upload failed'
            ff_utils.patch_metadata(new_file, original_file['uuid'], key=ff_key)

    # nothing to patch to ff_meta
    return None


def metadata_only(event):
    # just create a fake awsem config so the handler function does it's magic
    '''
    if not event.get('args'):
        event['args'] = {'app_name': event['ff_meta'].get('awsem_app_name'),
                         'output_S3_bucket': 'metadata_only',
                         'output_target': {'metadata_only': 'metadata_only'}
                         }

    if not event.get('config'):
        event['config'] = {'runmode': 'metadata_only'}
    '''

    return real_handler(event, None)


@utils.powerup('update_ffmeta_awsem', metadata_only)
def handler(event, context):
    return real_handler(event, context)


def real_handler(event, context):
    # check the status and other details of import
    '''
    this is to check if the task run is done:
    http://docs.sevenbridges.com/reference#get-task-execution-details
    '''
    # get data
    # used to automatically determine the environment
    tibanna_settings = event.get('_tibanna', {})
    tibanna = utils.Tibanna(**tibanna_settings)
    # sbg = sbg_utils.create_sbg_workflow(token=tibanna.sbg_keys, **event.get('workflow'))
    ff_meta = ff_utils.create_ffmeta_awsem(app_name=event.get('ff_meta').get('awsem_app_name'), **event.get('ff_meta'))
    pf_meta = event.get('pf_meta')
    # ensure this bad boy is always initialized
    patch_meta = False
    awsem = ec2_utils.Awsem(event)

    # go through this and replace export_report with awsf format
    # actually interface should be look through ff_meta files and call
    # give me the status of this thing from the runner, and runner.output_files.length
    # so we just build a runner with interface to sbg and awsem
    # runner.output_files.length()
    # runner.output_files.file.status
    # runner.output_files.file.loc
    # runner.output_files.file.get

    awsem_output = awsem.output_files()
    ff_output = len(ff_meta.output_files)
    if len(awsem_output) != ff_output:
        ff_meta.run_status = 'error'
        ff_meta.description = "%d files output expected %s" % (ff_output, len(awsem_output))
        ff_meta.post(key=tibanna.ff_keys)
        raise Exception("Failing the workflow because outputed files = %d and ffmeta = %d" %
                        (awsem_output, ff_output))

    for _, export in awsem_output.iteritems():
        upload_key = export.key
        status = export.status
        print("export res is %s", status)
        if status == 'COMPLETED':
            patch_meta = OUTFILE_UPDATERS[export.output_type]('uploaded', export, ff_meta, tibanna)
            if pf_meta:
                pf_meta = update_processed_file_metadata('uploaded', pf_meta, tibanna)
        elif status in ['FAILED']:
            patch_meta = OUTFILE_UPDATERS[export.output_type]('upload failed', export, ff_meta, tibanna)
            ff_meta.run_status = 'error'
            ff_meta.post(key=tibanna.ff_keys)
            raise Exception("Failed to export file %s" % (upload_key))

    # if we got all the exports let's go ahead and update our ff_metadata object
    ff_meta.run_status = "complete"

    # allow for a simple way for updater to add appropriate meta_data
    if patch_meta:
        ff_meta.__dict__.update(patch_meta)

    # add postrunjson log file to ff_meta as a url
    ff_meta.awsem_postrun_json = get_postrunjson_url(event)

    # make all the file export meta-data stuff here
    # TODO: fix bugs with ff_meta mapping for output and input file
    try:
        ff_meta.post(key=tibanna.ff_keys)
    except:
        raise Exception("Failed to update run_status")

    event['ff_meta'] = ff_meta.as_dict()
    event['pf_meta'] = pf_meta

    return event


def get_postrunjson_url(event):
    logbucket = event['config']['log_bucket']
    jobid = event['jobid']
    postrunjson_url = 'https://s3.amazonaws.com/' + logbucket + '/' + jobid + '.postrun.json'
    return postrunjson_url


# Cardinal knowledge of all workflow updaters
OUTFILE_UPDATERS = defaultdict(lambda: donothing)
OUTFILE_UPDATERS['Output report file'] = md5_updater
OUTFILE_UPDATERS['Output QC file'] = qc_updater