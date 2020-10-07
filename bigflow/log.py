import logging
import sys

from textwrap import dedent

from google.cloud import logging_v2
from urllib.parse import quote_plus


def create_logging_client():
    return logging_v2.LoggingServiceV2Client()


class GCPLoggerHandler(logging.StreamHandler):

    def __init__(self, project_id, logger_name, workflow_id):
        logging.StreamHandler.__init__(self)

        self.client = create_logging_client()
        self.project_id = project_id
        self.workflow_id = workflow_id
        self.logger_name = logger_name

        self._log_entry_prototype = logging_v2.types.LogEntry(
            log_name=f"projects/{self.project_id}/logs/{self.logger_name}",
            labels={
                "id": str(self.workflow_id or self.project_id),
            },
            resource={
                "type": "global",
                "labels": {
                    "project_id": str(self.project_id),
                },
            },
        )

    def emit(self, record: logging.LogRecord):
        cl_log_level = record.levelname  # CloudLogging list of supported log levels is a superset of python logging level names
        self.write_log_entries(record.getMessage(), cl_log_level)

    def write_log_entries(self, message, severity):
        entry = logging_v2.types.LogEntry()
        entry.CopyFrom(self._log_entry_prototype)
        entry.text_payload = message
        entry.severity = severity
        self.client.write_log_entries([entry])


def _uncaught_exception_handler(logger):
    def handler(exception_type, value, traceback):
        logger.error(f'Uncaught exception: {value}', exc_info=(exception_type, value, traceback))
    return handler


class BigflowLogging(object):
    IS_LOGGING_SET = False

    @staticmethod
    def configure_logging(project_id, logger_name, workflow_id=None):

        if BigflowLogging.IS_LOGGING_SET:
            import warnings
            warnings.warn(UserWarning("bigflow.log is is already configured - skip"))
            return

        logging.basicConfig(level=logging.INFO)
        gcp_logger_handler = GCPLoggerHandler(project_id, logger_name, workflow_id)
        gcp_logger_handler.setLevel(logging.INFO)

        query = quote_plus(dedent(f'''
            logName="projects/{project_id}/logs/{logger_name}"
            labels.id="{workflow_id or project_id}"
        '''))
        logging.info(dedent(f"""
               *************************LOGS LINK*************************
               You can find this workflow logs here: https://console.cloud.google.com/logs/query;query={query}
               ***********************************************************
        """))

        logging.getLogger(None).addHandler(gcp_logger_handler)
        sys.excepthook = _uncaught_exception_handler(logging.getLogger())
        
        BigflowLogging.IS_LOGGING_SET = True
