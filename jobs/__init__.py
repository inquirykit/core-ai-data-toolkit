"""
Jobs: long-running scripts (training, batch work) run in their own process.

A job file in a subfolder of jobs/ registers a Job subclass with
``@register_job``; see jobs/README.md. Only pydantic is imported here, since
the worker imports this package in the job's environment too.
"""

from .base import DataFile, Job, JobContext, JobParams, JobResult
from .registry import register_job

__all__ = ["DataFile", "Job", "JobContext", "JobParams", "JobResult", "register_job"]
