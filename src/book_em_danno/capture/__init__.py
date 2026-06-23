"""Wire-traffic capture for `danno --capture`.

`proxy` is a recording-and-re-originating HTTP proxy; `wiring` redirects a config's
backend base_urls at per-backend proxies and threads the egress allow-list. Together
they let danno record the request+response traffic between the sandboxed agent and
its model backends without a manual proxy dance.
"""
