"""Local stand-in for the Twitter REST endpoints this app calls.

The stand-in replaces only the socket send. Tweepy still builds the URL,
query string, and OAuth 1 Authorization header, and requests still applies
that header in PreparedRequest.prepare_auth before Session.send.

Protected timeline and follow-list bodies are returned only when the request
is signed with the operator user access token. That matches the v1.1 rule
that a protected user timeline is available to the authenticated user who
owns it or follows it, and not to application-only credentials. Any other
caller receives an error object and a non-2xx status, which is the shape
Tweepy's ModelParser.parse_error reads.
"""

import json
import os
import threading
from urllib.parse import parse_qs, urlparse

import requests

OPERATOR_CONSUMER_KEY = "PLACEHOLDER_CONSUMER_KEY"
OPERATOR_CONSUMER_SECRET = "PLACEHOLDER_CONSUMER_SECRET"
OPERATOR_ACCESS_TOKEN = "PLACEHOLDER_ACCESS_TOKEN"
OPERATOR_ACCESS_TOKEN_SECRET = "PLACEHOLDER_ACCESS_TOKEN_SECRET"

PROTECTED_TWEET_TEXT = "PROTECTED_TIMELINE_ONLY_VISIBLE_TO_TOKEN_OWNER"
PROTECTED_FRIEND_SCREEN_NAME = "protectedfriend"
PROTECTED_FRIEND_NAME = "Protected Friend"
PROTECTED_FRIEND_FOLLOWERS = 424242
PROTECTED_FRIEND_ID = 1001

API_HOST = "api.twitter.com"
CREATED_AT = "Thu Apr 06 15:28:43 +0000 2017"

LOG = []
_LOCK = threading.Lock()
_INSTALLED = False


def credentials_csv_line():
    return ", ".join(
        [
            OPERATOR_CONSUMER_KEY,
            OPERATOR_CONSUMER_SECRET,
            OPERATOR_ACCESS_TOKEN,
            OPERATOR_ACCESS_TOKEN_SECRET,
        ]
    )


def reset():
    with _LOCK:
        LOG.clear()
        path = os.environ.get("TWITTER_STANDIN_LOG")
        if path:
            open(path, "w").close()


def calls():
    path = os.environ.get("TWITTER_STANDIN_LOG")
    if path and os.path.exists(path):
        rows = []
        with open(path) as handle:
            for line in handle:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows
    with _LOCK:
        return list(LOG)


def user_token_calls():
    return [
        call for call in calls() if call.get("oauth_token") == OPERATOR_ACCESS_TOKEN
    ]


def install():
    global _INSTALLED
    if _INSTALLED:
        return
    requests.Session.send = _send
    _INSTALLED = True


def _header_param(header, name):
    if not header:
        return None
    needle = name + '="'
    start = header.find(needle)
    if start < 0:
        return None
    start += len(needle)
    end = header.find('"', start)
    if end < 0:
        return None
    return header[start:end]


def _record(request):
    parsed = urlparse(request.url)
    query = {
        key: values[-1]
        for key, values in parse_qs(parsed.query, keep_blank_values=True).items()
    }
    authorization = request.headers.get("Authorization")
    entry = {
        "method": request.method,
        "url": request.url,
        "host": parsed.hostname,
        "path": parsed.path,
        "query": query,
        "oauth_token": _header_param(authorization, "oauth_token"),
        "oauth_consumer_key": _header_param(authorization, "oauth_consumer_key"),
    }
    with _LOCK:
        LOG.append(entry)
        path = os.environ.get("TWITTER_STANDIN_LOG")
        if path:
            with open(path, "a") as handle:
                handle.write(json.dumps(entry) + "\n")
    return entry


def _response(request, status, payload, rate_remaining):
    response = requests.Response()
    response.status_code = status
    response.reason = "OK" if status == 200 else "Error"
    response.url = request.url
    response.request = request
    response.encoding = "utf-8"
    body = payload if isinstance(payload, str) else json.dumps(payload)
    response._content = body.encode("utf-8")
    response.headers["Content-Type"] = "application/json"
    response.headers["x-rate-limit-limit"] = "15"
    response.headers["x-rate-limit-remaining"] = str(rate_remaining)
    response.headers["x-rate-limit-reset"] = "1"
    return response


def _unauthorized():
    return {
        "errors": [
            {
                "code": 179,
                "message": "Sorry, you are not authorized to see this status.",
            }
        ]
    }


def _send(self, request, **kwargs):
    del self, kwargs
    entry = _record(request)
    if entry["host"] != API_HOST or not entry["url"].startswith("https://"):
        raise AssertionError("stand-in refused a non-Twitter URL: %s" % request.url)

    user_context = entry["oauth_token"] == OPERATOR_ACCESS_TOKEN
    path = entry["path"]
    query = entry["query"]
    remaining = max(0, 15 - len(calls()))

    if path == "/1.1/statuses/user_timeline.json":
        if not user_context:
            return _response(request, 401, _unauthorized(), remaining)
        if "max_id" in query:
            return _response(request, 200, [], remaining)
        tweet_id = 850007368138018817
        return _response(
            request,
            200,
            [
                {
                    "created_at": CREATED_AT,
                    "id": tweet_id,
                    "id_str": str(tweet_id),
                    "text": PROTECTED_TWEET_TEXT,
                    "retweet_count": 0,
                    "truncated": False,
                    "entities": {"hashtags": [], "symbols": [], "user_mentions": [], "urls": []},
                    "user": {"id": 1, "screen_name": query.get("id", ""), "protected": True},
                }
            ],
            remaining,
        )

    if path == "/1.1/friends/ids.json":
        if not user_context:
            return _response(request, 401, _unauthorized(), remaining)
        return _response(
            request,
            200,
            {
                "previous_cursor": 0,
                "previous_cursor_str": "0",
                "next_cursor": 0,
                "next_cursor_str": "0",
                "ids": [PROTECTED_FRIEND_ID],
            },
            remaining,
        )

    if path == "/1.1/users/show.json":
        if not user_context:
            return _response(request, 401, _unauthorized(), remaining)
        return _response(
            request,
            200,
            {
                "id": int(query.get("id", "0")),
                "id_str": query.get("id", "0"),
                "name": PROTECTED_FRIEND_NAME,
                "screen_name": PROTECTED_FRIEND_SCREEN_NAME,
                "followers_count": PROTECTED_FRIEND_FOLLOWERS,
                "created_at": CREATED_AT,
                "profile_image_url_https": "https://example.invalid/protected.png",
                "protected": True,
            },
            remaining,
        )

    return _response(request, 404, _unauthorized(), remaining)
