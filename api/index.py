from http.server import BaseHTTPRequestHandler

from trendradar.assistant.web import AssistantHTTPRequestHandler


class handler(AssistantHTTPRequestHandler, BaseHTTPRequestHandler):
    """Vercel Python runtime entrypoint.

    Keep an explicit BaseHTTPRequestHandler base so static entrypoint analyzers
    can reliably classify this file as a Python Serverless Function.
    """

    pass
