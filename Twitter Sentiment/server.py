"""
A server that responds with two pages, one showing the most recent
100 tweets for given user and the other showing the followers
of a given user (sorted by the number of followers those users have).
For authentication purposes, the server takes a commandline argument
that indicates the file containing Twitter data in a CSV file format:
    consumer_key, consumer_secret, access_token, access_token_secret
For example, I pass in my secrets via file name:
    /Users/parrt/Dropbox/licenses/twitter.csv
Please keep in mind the limits imposed by the twitter API:
    https://dev.twitter.com/rest/public/rate-limits
For example, you can only do 15 follower list fetches per
15 minute window, but you can do 900 user timeline fetches.
"""
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from flask import Flask, render_template, abort
from tweetie import *
from colour import Color

from numpy import median

app = Flask(__name__)

# A request never waits on Twitter longer than this, so a stalled call cannot
# pin one of the server's few worker threads.
REQUEST_DEADLINE_SECONDS = 2.5
# Calls abandoned at the deadline keep running until tweepy's socket timeout;
# this bounds how many can be outstanding before new requests are refused.
MAX_PENDING_TWITTER_CALLS = 16

_twitter_pool = ThreadPoolExecutor(max_workers=MAX_PENDING_TWITTER_CALLS)
_twitter_slots = threading.BoundedSemaphore(MAX_PENDING_TWITTER_CALLS)


def call_twitter(fn, *args):
    if not _twitter_slots.acquire(blocking=False):
        abort(503)
    try:
        future = _twitter_pool.submit(fn, *args)
    except BaseException:
        _twitter_slots.release()
        raise
    future.add_done_callback(lambda _f: _twitter_slots.release())
    try:
        return future.result(timeout=REQUEST_DEADLINE_SECONDS)
    except FutureTimeout:
        abort(504)

def add_color(tweets):
    """
    Given a list of tweets, one dictionary per tweet, add
    a "color" key to each tweets dictionary with a value
    containing a color graded from red to green. Pure red
    would be for -1.0 sentiment score and pure green would be for
    sentiment score 1.0.
    Use colour.Color to get 100 color values in the range
    from red to green. Then convert the sentiment score from -1..1
    to an index from 0..100. That index gives you the color increment
    from the 100 gradients.
    This function modifies the dictionary of each tweet. It lives in
    the server script because it has to do with display not collecting
    tweets.
    """
    colors = list(Color("red").range_to(Color("green"), 100))
    for t in tweets:
        score = t['score']
        t['color'] = colors[int(round((score+1)/2*99))] # scale -1..1 to 0..99


@app.route("/<name>")
def tweets(name):
    "Display the tweets for a screen name color-coded by sentiment score"
    record = call_twitter(fetch_tweets, api, name)
    add_color(record['tweets'])
    median_score = median([record['tweets'][i]['score'] for i in range(len(record['tweets']))])

    return render_template('tweets.html', record=record, median=median_score)

@app.route("/following/<name>")
def following(name):
    """
    Display the list of users followed by a screen name, sorted in
    reverse order by the number of followers of those users.
    """
    following = call_twitter(fetch_following, api, name)
    following = sorted(following, key=lambda t: t['followers'], reverse=True)
    return render_template('following.html', following = following, name = name)

i = sys.argv.index('server:app')
twitter_auth_filename = sys.argv[i+1] # e.g., "/Users/parrt/Dropbox/licenses/twitter.csv"
#twitter_auth_filename = sys.argv[1]
api = authenticate(twitter_auth_filename)

#app.run(host='0.0.0.0', port=5000)

#sudo python2 server.py /Users/Ben/Dropbox/License/twitter.csv
# gunicorn -D --threads 4 -b 0.0.0.0:5000 --access-logfile server.log server:app twitter.csv


# record = fetch_tweets(api, 'benliu91MSAN692')
#
# for t in record['tweets']:
#     print t['text']


#scp to aws:
# scp -i bliu36msan.pem -r twitter.csv ec2-user@34.212.220.211:sentiment-benliu91