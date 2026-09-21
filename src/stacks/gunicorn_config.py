"""Gunicorn configuration for Stacks."""
import multiprocessing
import signal
import sys

# Server socket
bind = "0.0.0.0:7788"

# Worker processes
workers = 4
# Threaded workers: a sync worker is killed after `timeout` seconds of a single
# request, which would cut off large library archive downloads mid-stream.
worker_class = "gthread"
threads = 4
worker_connections = 1000
timeout = 120
keepalive = 5

# Logging
accesslog = None
errorlog = "-"
loglevel = "info"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s"'

# Process naming
proc_name = "stacks"

# Server mechanics
daemon = False
pidfile = None
user = None
group = None
tmp_upload_dir = None


def on_starting(server):
    """Called just before the master process is initialized."""
    print("◼ Gunicorn starting...")
    sys.stdout.flush()


def when_ready(server):
    """Called just after the server is started."""
    print("◼ Stacks server ready")
    sys.stdout.flush()


def on_exit(server):
    """Called just before exiting Gunicorn."""
    print("◼ Gunicorn shutting down...")
    sys.stdout.flush()


def worker_exit(server, worker):
    """Called just after a worker has been exited."""
    # Cleanup happens automatically when the worker process exits
    # The Flask app's worker and queue objects will be garbage collected
    pass
