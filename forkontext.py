import json
import re
import sys
import urllib.parse
import email.utils
import datetime
from collections import namedtuple

from flask import Flask, request, url_for, jsonify, make_response
from sqlalchemy import Table, Column, create_engine
from sqlalchemy.types import PickleType, Integer, String, DateTime
from sqlalchemy.sql import select
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

import bleach
import mf2py
import mf2util
import requests
from cachecontrol import CacheControl
from cachecontrol.caches import FileCache

app = Flask(__name__)
app.config.from_pyfile('forkontext.cfg')


bleach.ALLOWED_TAGS += [
    'a', 'img', 'p', 'br', 'marquee', 'blink',
    'audio', 'video', 'table', 'tbody', 'td', 'tr', 'div', 'span',
    'pre', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
]

bleach.ALLOWED_ATTRIBUTES.update({
    'img': ['src', 'alt', 'title'],
    'audio': ['preload', 'controls', 'src'],
    'video': ['preload', 'controls', 'src'],
    'td': ['colspan'],
})

USER_AGENT = 'Fork On Text (https://github.com/kylewm/forkontext)'
TWITTER_RE = re.compile(
    r'https?://(?:www\.|mobile\.)?twitter\.com/(\w+)/status(?:es)?/(\w+)')


Base = declarative_base()


class Entry(Base):
    __tablename__ = 'entry'
    id = Column(Integer, primary_key=True)
    url = Column(String(1024))
    expiry = Column(DateTime)
    response = Column(PickleType)

    def __repr__(self):
        return 'Entry[url={}, expiry={}, response={}]'.format(
            self.url, self.expiry, self.response)


engine = create_engine(app.config['SQLALCHEMY_DATABASE_URI'])
Session = sessionmaker(bind=engine)

req_session = CacheControl(requests.session(),
                           cache=FileCache('.webcache'))


def init_db():
    Base.metadata.create_all(engine)


def fetch(url):
    try:
        session = Session()
        now = datetime.datetime.utcnow()
        cached = session.query(Entry).filter_by(url=url).first()

        app.logger.debug('check for cached response %s', cached)

        if not cached or now >= cached.expiry:
            if not cached:
                cached = Entry(url=url)
                session.add(cached)

            resp = req_session.get(url)
            if resp.status_code // 100 != 2:
                app.logger.warn('failed to fetch %s. response: %s - %s',
                                url, resp, resp.text)
                # do not update a previous good response
                if not cached.response or cached.response.status_code // 100 != 2:
                    cached.response = resp
                cached.expiry = now + datetime.timedelta(hours=1)
            else:
                cached.response = resp
                cached.expiry = now + datetime.timedelta(hours=12)

        session.commit()
        return cached.response
    except:
        session.rollback()
        raise
    finally:
        session.close()


def maybe_proxy(url):
    if ('TWITTER_AU_KEY' in app.config
            and 'TWITTER_AU_SECRET' in app.config):
        # swap out the a-u url for twitter urls
        match = TWITTER_RE.match(url)
        if match:
            proxy_url = (
                'https://twitter-activitystreams.appspot.com/@me/@all/@app/{}?'
                .format(match.group(2)) + urllib.parse.urlencode([
                    ('format', 'html'),
                    ('access_token_key', app.config['TWITTER_AU_KEY']),
                    ('access_token_secret', app.config['TWITTER_AU_SECRET']),
                ]))
            app.logger.debug('proxied twitter url %s', proxy_url)
            return proxy_url
    return url


@app.route('/')
def fetch_context():
    url = request.args.get('url')
    if not url:
        return make_response(jsonify({
            'error': 'missing_url',
            'message': "Missing 'url' query parameter",
        }), 400)

    # TODO cache everything. check newer urls more frequently than
    # older urls. be careful not to overwrite previous good responses
    # with failure.

    url = maybe_proxy(url)
    resp = fetch(url)

    if resp.status_code // 100 != 2:
        return make_response(jsonify({
            'error': 'fetch_failed',
            'message': 'Failed to fetch resource at ' + url,
            'response': resp.text,
            'code': resp.status_code,
        }), resp.status_code)

    parsed = mf2py.parse(
        doc=resp.text if 'content-type' in resp.headers else resp.content,
        url=url)
    entry = mf2util.interpret(parsed, url, want_json=True)

    blob = {}
    if entry:
        blob['data'] = entry

    cb = request.args.get('callback')
    if cb:  # jsonp
        resp = make_response('{}({})'.format(cb, json.dumps(blob)))
        resp.headers['content-type'] = 'application/javascript; charset=utf-8'
        return resp

    return jsonify(blob)


def to_html(entry):
    """:deprecated:"""
    if not entry:
        return None

    html = '<div class="h-cite">'
    foot = '</div>'

    if 'author' in entry:
        author_html = '<div class="p-author h-card">'
        author_foot = '</div>'
        if 'url' in entry['author']:
            author_html += '<a class="u-url" href="{}">'.format(
                entry['author']['url'])
            author_foot = '</a>' + author_foot
        if 'photo' in entry['author']:
            author_html += '<img src="{}" />'.format(
                entry['author']['photo'])
        if 'name' in entry['author']:
            author_html += '<span class="p-name">{}</span>'.format(
                entry['author']['name'])
        html += author_html + author_foot

    if 'name' in entry:
        html += '<h1 class="p-name">{}</h1>'.format(entry['name'])
    if 'content' in entry:
        html += '<div class="{}e-content">{}</div>'.format(
            'p-name ' if 'name' not in entry else '', entry['content'])

    permalink = ''
    permalink_foot = ''

    if 'url' in entry:
        permalink += '<a class="u-url" href="{}">'.format(entry['url'])
        permalink_foot += '</a>'

    if 'published' in entry:
        published = entry['published']
        try:
            pubdate = mf2util.parse_dt(published)
            pubiso = pubdate.isoformat()
            pubpretty = pubdate.strftime('%c')
        except:
            app.logger.warning('failed to parse datetime: ' + published,
                               exc_info=True)
            pubiso = pubpretty = published
        permalink += '<time datetime="{}">{}</time>'.format(pubiso, pubpretty)
    else:
        permalink += 'link'

    html += permalink + permalink_foot
    return html + foot


if __name__ == '__main__':
    app.run(debug=True)
