import base64
import boto3
import database
from flask import Flask, jsonify, redirect, render_template, request, url_for
from hashlib import sha256
import logging
import pika
import pika.exceptions
from sqlalchemy.sql import functions
import time
from werkzeug.utils import secure_filename


app = Flask(__name__)


# SQL database
logging.info("Connecting to SQL database")
engine, SQLSession = database.connect()

if not engine.dialect.has_table(engine.connect(), 'experiments'):
    logging.warning("The tables don't seem to exist; creating")
    from database import Base

    Base.metadata.create_all(bind=engine)


# AMQP
class AMQP(object):
    def __init__(self):
        self._connect()

    def _connect(self):
        logging.info("Connecting to AMQP broker")
        self.connection = pika.BlockingConnection(pika.ConnectionParameters(
            host='reproserver-rabbitmq',
            credentials=pika.PlainCredentials('admin', 'hackme')))
        self.channel = self.connection.channel()

        self.channel.queue_declare(queue='build_queue', durable=True)

    def publish_build_task(self, body):
        while True:
            try:
                self.channel.basic_publish('', routing_key='build_queue',
                                           body=body)
                return
            except pika.exceptions.ConnectionClosed:
                logging.exception("AMQP connection is down...")
                time.sleep(1)
            self._connect()


amqp = AMQP()


# Object storage
s3 = boto3.resource('s3', endpoint_url='http://reproserver-minio:9000',
                    aws_access_key_id='admin',
                    aws_secret_access_key='hackmehackme')

it = iter(s3.buckets.all())
try:
    next(it)
except StopIteration:
    for name in ['experiments', 'inputs', 'outputs']:
        s3.create_bucket(Bucket=name)


@app.route('/')
def index():
    """Landing page from which a user can select an experiment to unpack.
    """
    return render_template('index.html')


@app.route('/upload', methods=['POST'])
def unpack():
    """Target of the landing page.

    An experiment has been provided, store it and start the build process.
    """
    # Get uploaded file
    uploaded_file = request.files['rpz_file']
    assert uploaded_file.filename
    app.logger.info("Incoming file: %r", uploaded_file.filename)
    filename = secure_filename(uploaded_file.filename)

    # Hash it
    hasher = sha256()
    chunk = uploaded_file.read(4096)
    while chunk:
        hasher.update(chunk)
        chunk = uploaded_file.read(4096)
    filehash = hasher.hexdigest()
    app.logger.info("Computed hash: %s", filehash)

    # Rewind it
    uploaded_file.seek(0, 0)

    # Check for existence of experiment
    session = SQLSession()
    experiment = session.query(database.Experiment).get(filehash)
    if experiment:
        experiment.last_access = functions.now()
        app.logger.info("File exists in storage")
    else:
        # Insert it on S3
        s3.Object('experiments', filehash).put(Body=uploaded_file)
        app.logger.info("Inserted file in storage")

        # Insert it in database
        experiment = database.Experiment(hash=filehash)
        session.add(experiment)
    session.add(database.Upload(experiment=experiment,
                                filename=filename,
                                submitted_ip=request.remote_addr))
    session.commit()

    # Encode hash + filename for permanent URL
    experiment_code = base64.urlsafe_b64encode(filehash + '|' + filename)

    # Redirect to build page
    return redirect(url_for('reproduce', experiment_code=experiment_code), 302)


@app.route('/reproduce/<experiment_code>')
def reproduce(experiment_code):
    """Show build log and ask for run parameters.
    """
    # Decode info from URL
    app.logger.info("Decoding %r", experiment_code)
    try:
        permanent_id = base64.urlsafe_b64decode(
            experiment_code.encode('ascii'))
        sep = permanent_id.index('|')
    except Exception:
        return render_template('setup_notfound.html'), 404
    filehash = permanent_id[:sep]
    filename = permanent_id[sep + 1:]

    # Look up the experiment in database
    session = SQLSession()
    experiment = session.query(database.Experiment).get(filehash)
    if not experiment:
        return render_template('setup_notfound.html'), 404
    # Also updates last access
    experiment.last_access = functions.now()

    try:
        # JSON endpoint, returns data for JavaScript to update the page
        if (request.accept_mimetypes.best_match(['application/json',
                                                 'text/html']) ==
                'application/json'):
            log_from = request.args.get('log_from', 0)
            return jsonify({'status': experiment.status,
                            'log': experiment.get_log(log_from),
                            'params': experiment.parameters})
        # HTML view, return the page
        else:
            # If it's done building, send build log and run form
            if experiment.status == database.Status.BUILT:
                app.logger.info("Experiment already built")
                return render_template('setup.html', filename=filename,
                                       built=True, error=False,
                                       log=experiment.get_log(0),
                                       params=experiment.parameters,
                                       experiment_code=experiment_code)
            if experiment.status == database.Status.ERROR:
                app.logger.info("Experiment is errored")
                return render_template('setup.html', filename=filename,
                                       built=True, error=True,
                                       log=experiment.get_log(0),
                                       experiment_code=experiment_code)
            # If it's currently building, show the log
            elif experiment.status == database.Status.BUILDING:
                app.logger.info("Experiment is currently building")
                return render_template('setup.html', filename=filename,
                                       built=False, log=experiment.get_log(0),
                                       experiment_code=experiment_code)
            # Else, trigger the build
            else:
                if experiment.status == database.Status.NOBUILD:
                    app.logger.info("Triggering a build, sending message")
                    experiment.status = database.Status.QUEUED
                    amqp.publish_build_task(filehash)
                return render_template('setup.html', filename=filename,
                                       built=False,
                                       experiment_code=experiment_code)
    finally:
        session.commit()


@app.route('/run/<experiment_code>', methods=['POST'])
def run(experiment_code):
    """Gets the run parameters POSTed to from /reproduce.

    Triggers the run and redirects to the results page.
    """
    # Decode info from URL
    app.logger.info("Decoding %r", experiment_code)
    try:
        permanent_id = base64.urlsafe_b64decode(
            experiment_code.encode('ascii'))
        sep = permanent_id.index('|')
    except Exception:
        return render_template('setup_notfound.html'), 404
    filehash = permanent_id[:sep]
    filename = permanent_id[sep + 1:]

    # Get run parameters
    params = {}
    for k, v in request.args.iteritems():
        if k.startswith('param_'):
            params[k[6:]] = v

    # TODO: Trigger run
    return ("Not yet implemented: run experiment {filehash} {filename}\n"
            "Parameters:\n{params}\n").format(
                filehash=filehash, filename=filename,
                params="  (no parameters)" if not params else
                '\n'.join("  - {k}: {v}".format(k=k, v=v)
                          for k, v in params.iteritems()))


@app.route('/about')
def about():
    return render_template('about.html')


@app.route('/data')
def data():
    """Print some system information.
    """
    session = SQLSession()
    return render_template(
        'data.html',
        experiments=session.query(database.Experiment).all(),
    )


def main():
    # Start webserver
    app.logger.info("web running")
    app.run(host="0.0.0.0", port=8000, debug=True)
