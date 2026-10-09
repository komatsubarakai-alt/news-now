"""Bounded source lookup and JSON Responses client (standard library only)."""
import json
import os
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urlencode


class BudgetExceeded(RuntimeError):
    pass


class AI:
    def __init__(self, limit=30):
        self.remaining = limit

    def ask(self, prompt):
        if self.remaining <= 0:
            raise BudgetExceeded('追跡AIの実行上限に達しました')
        self.remaining -= 1
        data = json.dumps({'model': os.environ.get('TRACKING_MODEL', 'gpt-5.4-mini'),
                           'input': prompt, 'max_output_tokens': 3500}).encode()
        request = urllib.request.Request('https://api.openai.com/v1/responses', data=data,
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + os.environ['OPENAI_API_KEY']})
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.load(response)
        chunks = [content['text'] for output in payload.get('output', [])
                  for content in output.get('content', []) if content.get('type') == 'output_text']
        text = ''.join(chunks).strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        try:
            result = json.loads(text)
        except json.JSONDecodeError as exc:
            exc.ai_response_excerpt = text[:8000]
            raise
        if not isinstance(result, dict):
            exc = ValueError('JSON object required')
            exc.ai_response_excerpt = text[:8000]
            raise exc
        return result


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
