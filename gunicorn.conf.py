"""Gunicorn settings, read automatically from the working directory.

The Render service starts gunicorn without --threads, which leaves one request at a time. A slow
page then blocks every other request, including the health check, and visitors see a bad gateway.
Four threads in one worker let requests overlap while the in-process scheduler keeps a single copy.
"""
workers = 1
threads = 4
timeout = 120
