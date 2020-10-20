import base64
import functools
import datetime
import time
import inspect
import io
import os
import pathlib
import pickle
import random
import re
import string
import subprocess
import textwrap
import time
import typing
import contextlib
import logging

from google.cloud import dataproc_v1, storage

import bigflow
import bigflow.configuration
import bigflow.resources
import bigflow.commons


logger = logging.getLogger(__name__)

__all__ = [
    'PySparkJob',
]

DEFAULT_REQUIREMENTS = [
    f"bigflow[dataproc,log,bigquery]=={bigflow.__version__}",
]


class PySparkJob:

    def __init__(
        self,
        id,
        driver_callable: typing.Callable,
        bucket_id: str,
        gcp_project_id: str,
        gcp_region: str,
        driver_arguments: typing.Optional[dict] = None,
        pip_packages: typing.Union[typing.Iterable[str], pathlib.Path] = (),
        jar_file_uris: typing.Iterable[str] = ('gs://spark-lib/bigquery/spark-bigquery-latest_2.12.jar',),
        worker_num_instances: int = 2,
        worker_machine_type: str = 'n1-standard-1',
        env: typing.Optional[str] = None,
        setup_file: typing.Optional[str] = None,
    ):
        self.id = id

        self.driver_filename = 'driver.py'
        self.driver_callable = driver_callable
        self.driver_arguments = driver_arguments or {}

        self.bucket_id = bucket_id
        self.gcp_project_id = gcp_project_id
        self.gcp_region = gcp_region

        self.env = env or bigflow.configuration.current_env() or 'none'

        self.jar_file_uris = jar_file_uris
        self.worker_machine_type = worker_machine_type
        self.worker_num_instances = worker_num_instances

        if isinstance(pip_packages, pathlib.Path):
            self.pip_packages = bigflow.resources.read_requirements(pip_packages)
        elif pip_packages:
            self.pip_packages = pip_packages
        else:
            self.pip_packages = DEFAULT_REQUIREMENTS

        if setup_file:
            self.setup_file = setup_file
        else:
            self.setup_file = None
            self._project = _capture_caller_topmodule()
            self._any_file_inside_project = _capture_caller_path(1)

    def _ensure_has_setup_file(self):
        if self.setup_file:
            return
        logger.debug("Find or create setup file for main package")
        self.setup_file = bigflow.resources.find_or_create_setup_for_main_project_package(
            self._project,
            self._any_file_inside_project)

    def _generate_internal_jobid(self, runtime):
        job_random_suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
        job_timestamp = datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S")  # FIXME: use 'runtime' ?
        jobid = f"{self.id.lower()}-{self.env or 'none'}-{job_timestamp}-{job_random_suffix}"
        logger.info("Internal job is %r", jobid)
        return jobid

    def _prepare_driver_callable(self, runtime):
        kwargs = dict(self.driver_arguments)
        kwargs['runtime'] = runtime
        
        envs = {}
        if self.env:
            logger.debug("Will set env variable 'env' on pyspark driver to %r", self.env)
            envs['env'] = self.env

        return _PySparkDriverWrapper(self.driver_callable, (), kwargs, envs)

    @contextlib.contextmanager
    def _with_temp_cluster(self, cluster_name):
        client_options = {'api_endpoint': f"{self.gcp_region}-dataproc.googleapis.com:443"}
        dataproc_cluster_client = dataproc_v1.ClusterControllerClient(client_options=client_options)
        try:
            logger.debug("Create temp cluster %r", cluster_name)
            cluster_name = _create_cluster(
                dataproc_cluster_client=dataproc_cluster_client,
                project_id=self.gcp_project_id,
                region=self.gcp_region,
                cluster_name=cluster_name,
                requirements=self.pip_packages,
                worker_machine_type=self.worker_machine_type,
                worker_num_instances=self.worker_num_instances,
            )
            yield cluster_name
        finally:
            logger.debug("Delete temp cluster %r", cluster_name)
            _delete_cluster(dataproc_cluster_client, self.gcp_project_id, self.gcp_region, cluster_name)

    def _prepare_pyspark_properties(self):
        ps = {
            'spark.app.name': self.id,
        }
        if self.env:
            ps['spark.workerEnv.bf_env'] = self.env            
            ps['spark.executorEnv.bf_env'] = self.env
        logger.debug("Pyspark properties are %r", ps)
        return ps

    def run(self, runtime):
        logger.info("Run job %r", self.id)

        self._ensure_has_setup_file()
        job_internal_id = self._generate_internal_jobid(runtime)

        client_options = {'api_endpoint': f"{self.gcp_region}-dataproc.googleapis.com:443"}
        storage_client = storage.Client(project=self.gcp_project_id)
        dataproc_job_client = dataproc_v1.JobControllerClient(client_options=client_options)

        driver = self._prepare_driver_callable(runtime)

        logger.info("Prapare and upload python package...")
        bucket = storage_client.get_bucket(self.bucket_id)
        egg_local_path = _build_project_egg(self.setup_file)
        egg_path = _upload_egg(egg_local_path, bucket, job_internal_id)

        driver_path = f"{job_internal_id}/{self.driver_filename}"
        generate_and_upload_driver(driver, bucket, driver_path)

        with self._with_temp_cluster(job_internal_id) as cluster_name:
            job = _submit_single_pyspark_job(
                dataproc_job_client=dataproc_job_client,
                project_id=self.gcp_project_id,
                region=self.gcp_region,
                cluster_name=cluster_name,
                bucket_id=self.bucket_id,
                jar_file_uris=self.jar_file_uris,
                driver_path=driver_path,
                egg_path=egg_path,
                properties=self._prepare_pyspark_properties(),
            )
            try:
                _wait_for_job_to_finish(dataproc_job_client, self.gcp_project_id, self.gcp_region, job)
            finally:
                _print_job_output_log(storage_client, dataproc_job_client, self.gcp_project_id, self.gcp_region, job)

        logger.info("Job %r was finished", self.id)


class _PySparkDriverWrapper:

    def __init__(self, callable, args, kwargs, envs):
        self.callable = callable
        self.args = args
        self.kwargs = kwargs
        self.envs = envs

    def __call__(self):
        # TODO(anjensan): Configure logging (depends on #90)
        os.environ.update(self.envs)
        self.callable(*self.args, **self.kwargs)


def generate_driver_script(callable):
    pickled = pickle.dumps(callable)
    return textwrap.dedent(f"""

        # Generated by `bigflow.dataproc.generate_and_upload_driver`
        import pickle, base64
        
        # unpickle'n'call {callable!r}
        data = {base64.b85encode(pickled)!r}

        call = pickle.loads(base64.b85decode(data))
        call()
    
    """)


def generate_and_upload_driver(callable, bucket, driver_path):
    logger.debug("Upload generate driver to %s", driver_path)
    driver_blob = bucket.blob(driver_path)
    script = generate_driver_script(callable)
    driver_blob.upload_from_string(content_type='application/octet-stream', data=script)


def _upload_egg(egg_local_path, bucket, job_internal_id):
    logger.debug("Upload egg to %s", egg_local_path)
    egg_path = '{}/{}'.format(job_internal_id, pathlib.Path(egg_local_path).name)
    egg_blob = bucket.blob(egg_path)
    egg_blob.upload_from_filename(filename=egg_local_path, content_type='application/octet-stream')
    return egg_path


def _wait_for_job_to_finish(dataproc_cluster_client, project_id, region, job_id):
    logger.info("Waiting for job to finish...")
    while True:
        job = dataproc_cluster_client.get_job(project_id=project_id, region=region, job_id=job_id)
        state = job.status.state
        if state == dataproc_v1.JobStatus.State.DONE:
            logger.info("Job is DONE")
            return job
        elif state == dataproc_v1.JobStatus.State.ERROR:
            logger.error("Job was failed with ERROR")
            raise Exception(job.status.details)
        elif state == dataproc_v1.JobStatus.State.CANCELLED:
            logger.warn("Job was CANCELLED")            
            raise Exception("Job was CANCELLED")
        time.sleep(5)


def _print_job_output_log(storage_client, dataproc_cluster_client, project_id, region, job_id):

    job = dataproc_cluster_client.get_job(project_id=project_id, region=region, job_id=job_id)
    log_buffer = io.BytesIO()
    # FIXME(anjensan): Download all logs
    storage_client.download_blob_to_file(job.driver_output_resource_uri + ".000000000", log_buffer)
    logger.info("JOB OUTPUT:\n=====\n%s\n=====", log_buffer.getvalue().decode())


def _create_cluster(dataproc_cluster_client, project_id, region, cluster_name, requirements, worker_num_instances, worker_machine_type):
    packages = " ".join(filter(None, requirements))
    cluster_data = {
        # "project_id": project_id,
        "cluster_name": cluster_name,
        "config": {
            "master_config": {
                "num_instances": 1,
                "machine_type_uri": "n1-standard-4",
                "disk_config": {
                    "boot_disk_size_gb": 100
                }},
            "worker_config": {
                "num_instances": worker_num_instances,
                "machine_type_uri": worker_machine_type,
                "disk_config": {
                    "boot_disk_size_gb": 100
                }},
            "software_config": {"image_version": "1.5"},
            "initialization_actions": [
                {"executable_file": "gs://goog-dataproc-initialization-actions-{}/python/pip-install.sh".format(region)}
            ],
            "gce_cluster_config": {
                "metadata": [('PIP_PACKAGES', packages)]
            }
        },
    }
    logger.debug("Cluster spec %r", cluster_data)

    start_at = time.time()
    logger.info("Create cluster %r", cluster_name)
    cluster_future = dataproc_cluster_client.create_cluster(
        project_id=project_id,
        region=region,
        cluster=cluster_data,
    )
    
    logger.info("Waiting for cluster creation...")
    cluster_future.result()

    passed = time.time() - start_at
    logger.info("Cluster created in %s seconds." % passed)

    return cluster_name


def _submit_single_pyspark_job(
        dataproc_job_client, 
        project_id,
        region,
        cluster_name,
        bucket_id,
        driver_path,
        egg_path,
        jar_file_uris,
        properties,
    ):
    jobspec = {
        "placement": {
            "cluster_name": cluster_name,
        },
        "pyspark_job": {
            'main_python_file_uri': f"gs://{bucket_id}/{driver_path}",
            'jar_file_uris': jar_file_uris,
            'python_file_uris': [f"gs://{bucket_id}/{egg_path}"],
            'properties': properties,
        },
    }
    logger.debug("Pyspark job %r", jobspec)
    result = dataproc_job_client.submit_job(project_id=project_id, region=region, job=jobspec)

    job_id = result.reference.job_id
    print("Job {} submitted.".format(job_id))
    print(f"https://console.cloud.google.com/dataproc/jobs/{job_id}?project={project_id}&region={region}")

    return job_id


def _delete_cluster(dataproc_cluster_client, project_id, region, cluster):
    print("Deleting cluster...")
    dataproc_cluster_client.delete_cluster(
        project_id=project_id,
        region=region,
        cluster_name=cluster,
    )
    print("Cluster was deleted.")


def _capture_caller_path(deep=1):
    return os.path.abspath(inspect.stack()[deep + 1].filename)


def _capture_caller_topmodule(deep=1):
    f = inspect.stack()[deep + 1].frame
    module = f.f_globals['__name__']
    return module.split(".", 2)[0]


def _build_project_egg(setup_file):
    cwd = pathlib.Path(setup_file).parent.resolve()
    output = bigflow.commons.run_process(
        ['python', setup_file, 'bdist_egg'], 
        cwd=str(cwd),
    )
    egg_filename = re.search('creating \'([^\']*)\'', output).group(1)
    logger.debug("Egg filename is %r", egg_filename)
    return str(cwd / egg_filename)