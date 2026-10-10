"""Bounded source lookup and JSON Responses client (standard library only)."""
import json
import os
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urlencode


from ai_cost_control import BudgetExceeded, response, json_answer, cached


class AI:
    def __init__(self, limit=30):
        self.remaining = limit

    def ask(self, prompt):
        model = os.environ.get('TRACKING_MODEL', 'gpt-5.4-mini')
        hit = cached(prompt, model)
        payload = response(prompt, model, allow_network=self.remaining > 0)
        if not hit:
            self.remaining -= 1
        return json_answer(payload)


def search(query):
    url = 'https://news.google.com/rss/search?' + urlencode({'q': query, 'hl': 'ja', 'gl': 'JP', 'ceid': 'JP:ja'})
    request = urllib.request.Request(url, headers={'User-Agent': 'news-now/1.0'})
    with urllib.request.urlopen(request, timeout=30) as response:
        root = ET.fromstring(response.read())
    from source_filter import parse_rss, prefilter, record_exclusions
    # Apply the same source policy to backfill/scheduled lookup before AI as to collection.
    kept, exclusions = prefilter(parse_rss(root))
    record_exclusions(exclusions, 'lookup:' + query)
    return kept[:20]
