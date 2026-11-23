"""Anonymous clients must not use the operator Twitter user token.

The tweets route and the following route take a screen name from the URL
and call Twitter with the access token loaded at process start. These tests
fail while that is still true. A fix passes them when an anonymous client
cannot cause a user-token call, cannot see protected timeline or follow-list
data, and cannot multiply Twitter calls by repeating one screen name.

Run from a virtualenv that has the libraries the app imports:

    pip install pytest 'Flask==2.2.5' 'Werkzeug==2.3.7' 'tweepy==3.10.0' \
        'requests==2.31.0' 'requests-oauthlib==1.3.1' 'oauthlib==3.2.2' \
        'colour==0.1.5' 'vaderSentiment==3.3.2' 'numpy==1.26.4' 'gunicorn==21.2.0'
    cd "Twitter Sentiment"
    python -m pytest tests/test_screen_name_authorization.py -v
"""

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import twitter_api_standin

SENTIMENT_DIR = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent


def _free_port():
    sock = socket.socket()
    sock.bind(("0.0.0.0", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture(scope="session")
def sentiment_app(tmp_path_factory):
    secret_dir = tmp_path_factory.mktemp("twitter-auth")
    csv_path = secret_dir / "twitter.csv"
    csv_path.write_text(twitter_api_standin.credentials_csv_line() + "\n")
    sys.argv = [
        "gunicorn",
        "--threads",
        "4",
        "-b",
        "0.0.0.0:5000",
        "server:app",
        str(csv_path),
    ]
    sys.path.insert(0, str(SENTIMENT_DIR))
    twitter_api_standin.install()
    import server

    return server


@pytest.fixture
def client(sentiment_app):
    twitter_api_standin.reset()
    return sentiment_app.app.test_client()


def _assert_no_user_token_and_no_marker(response, marker):
    signed = twitter_api_standin.user_token_calls()
    assert signed == [], signed
    body = response.get_data(as_text=True)
    assert marker not in body


@pytest.mark.parametrize("screen_name", ["ProtectedUser", "OtherProtected"])
def test_anonymous_tweets_route_does_not_use_user_token(client, screen_name):
    response = client.get("/" + screen_name)
    _assert_no_user_token_and_no_marker(
        response, twitter_api_standin.PROTECTED_TWEET_TEXT
    )


@pytest.mark.parametrize("screen_name", ["ProtectedUser", "OtherProtected"])
def test_anonymous_following_route_does_not_use_user_token(client, screen_name):
    response = client.get("/following/" + screen_name)
    _assert_no_user_token_and_no_marker(
        response, twitter_api_standin.PROTECTED_FRIEND_SCREEN_NAME
    )
    body = response.get_data(as_text=True)
    assert twitter_api_standin.PROTECTED_FRIEND_NAME not in body
    assert str(twitter_api_standin.PROTECTED_FRIEND_FOLLOWERS) not in body


def test_repeated_tweets_requests_do_not_multiply_twitter_calls(client):
    client.get("/ProtectedUser")
    after_first = len(twitter_api_standin.calls())
    for _ in range(4):
        client.get("/ProtectedUser")
    assert len(twitter_api_standin.calls()) == after_first
    assert twitter_api_standin.user_token_calls() == []


def test_repeated_following_requests_do_not_multiply_twitter_calls(client):
    client.get("/following/ProtectedUser")
    after_first = len(twitter_api_standin.calls())
    for _ in range(4):
        client.get("/following/ProtectedUser")
    assert len(twitter_api_standin.calls()) == after_first
    assert twitter_api_standin.user_token_calls() == []


def test_gunicorn_bind_serves_anonymous_routes_without_user_token(tmp_path):
    csv_path = tmp_path / "twitter.csv"
    csv_path.write_text(twitter_api_standin.credentials_csv_line() + "\n")
    call_log = tmp_path / "calls.jsonl"
    config_path = tmp_path / "gunicorn_conf.py"
    config_path.write_text(
        "import os, sys\n"
        "sys.path.insert(0, os.environ['TWITTER_SENTIMENT_TESTS'])\n"
        "def post_worker_init(worker):\n"
        "    import twitter_api_standin\n"
        "    twitter_api_standin.install()\n"
    )
    port = _free_port()
    env = os.environ.copy()
    env["TWITTER_SENTIMENT_TESTS"] = str(TESTS_DIR)
    env["TWITTER_STANDIN_LOG"] = str(call_log)
    access_log = tmp_path / "access.log"
    error_log = tmp_path / "error.log"
    command = [
        sys.executable,
        "-m",
        "gunicorn",
        "--threads",
        "4",
        "-b",
        "0.0.0.0:%d" % port,
        "-c",
        str(config_path),
        "--access-logfile",
        str(access_log),
        "--error-logfile",
        str(error_log),
        "server:app",
        str(csv_path),
    ]
    process = subprocess.Popen(
        command,
        cwd=str(SENTIMENT_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.time() + 20
        tweets = following = None
        last_error = None
        while time.time() < deadline:
            if process.poll() is not None:
                break
            try:
                tweets = _get("http://127.0.0.1:%d/ProtectedUser" % port)
                following = _get("http://127.0.0.1:%d/following/ProtectedUser" % port)
                break
            except (urllib.error.URLError, ConnectionError) as exc:
                last_error = exc
                time.sleep(0.2)
        else:
            raise AssertionError("gunicorn did not accept a connection: %s" % last_error)
        if process.poll() is not None:
            raise AssertionError(
                "gunicorn exited %s\n%s" % (process.returncode, error_log.read_text() if error_log.exists() else "")
            )
        assert tweets is not None and following is not None
        os.environ["TWITTER_STANDIN_LOG"] = str(call_log)
        try:
            signed = twitter_api_standin.user_token_calls()
            assert signed == [], signed
            assert twitter_api_standin.PROTECTED_TWEET_TEXT not in tweets
            assert twitter_api_standin.PROTECTED_FRIEND_SCREEN_NAME not in following
            assert twitter_api_standin.PROTECTED_FRIEND_NAME not in following
            assert str(twitter_api_standin.PROTECTED_FRIEND_FOLLOWERS) not in following
        finally:
            os.environ.pop("TWITTER_STANDIN_LOG", None)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def _get(url):
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.read().decode("utf-8", "replace")
