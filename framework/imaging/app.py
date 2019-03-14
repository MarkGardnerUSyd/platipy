from flask import Flask
from celery import current_app
from celery.bin import worker
from multiprocessing import Process
from loguru import logger
import json
import os
import uuid

# TODO configure log file properly
logger.add("logfile.log")

class Result():
    """Class used to return results to app from an algorithm"""

    dicom_path = None
    raw_files = None

    def __init__(self, dicom_path=None, raw_files=None):

        self.dicom_path = dicom_path
        self.raw_files = raw_files


class ImagingAlgorithm():

    def __init__(self, name, function, default_settings):
        self.name = name
        self.function = function
        self.default_settings = default_settings

    def settings_to_json(self):
        return json.dumps(self.default_settings, indent=4)


class FlaskApp(Flask):
    """
    Custom Flask App
    """

    working_dir = "./data"
    file_data = "data.json"
    data = {}
    algorithms = {}
    celery_started = False

    def __init__(self, name):

        super().__init__(name)

    def save_data(self):

        file_data_path = os.path.join(self.working_dir, self.file_data)

        with open(file_data_path, 'w') as outfile:
            json.dump(self.data, outfile, indent=4)

    def register(self, name, default_settings=None):

        def decorator(f):
            self.algorithms.update(
                {name: ImagingAlgorithm(name, f, default_settings)})
            return f

        return decorator

    def init_app(self):

        # Working directory
        # TODO put this in a config file
        if not os.path.exists(self.working_dir):
            os.mkdir(self.working_dir)

        file_data_path = os.path.join(self.working_dir, self.file_data)
        if not os.path.exists(file_data_path):
            self.data['endpoints'] = []
            self.save_data()

        with open(file_data_path) as json_file:
            self.data = json.load(json_file)

        # Clear endpoint tasks as after restart
        for e in self.data['endpoints']:
            if 'task_id' in e:
                # Revoke it incase it still exists
                revoke(e['task_id'], terminate=True)

                # And remove it from the dict
                e.pop('task_id', None)
        self.save_data()

    def run_celery(self):

        if self.celery_started:
            return

        application = current_app._get_current_object()

        celery_worker = worker.worker(app=application)

        options = {
            'broker': web_app.config['CELERY_BROKER_URL'],
            'loglevel': 'INFO',
            'traceback': True,
        }

        celery_worker.run(**options)

    def run(self, host=None, port=None, debug=None,
            load_dotenv=True, **options):

        self.init_app()

        logger.info('Starting APP!')

        p = Process(target=self.run_celery)
        p.start()
        self.celery_started = True

        super().run(host=host, port=port, debug=debug,
                    load_dotenv=load_dotenv, use_reloader=False, **options)

        p.join()

    def test_client(self, use_cookies=True, **kwargs):

        self.init_app()

        return super().test_client(use_cookies=use_cookies, **kwargs)


web_app = FlaskApp(__name__)
web_app.config['SECRET_KEY'] = uuid.uuid4().hex

import impit.framework.imaging.api
import impit.framework.imaging.views
import impit.framework.imaging.tasks
import impit.framework.imaging.models

# Import DataObject for easy import from algorithm
from impit.framework.imaging.models import DataObject