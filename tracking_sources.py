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
        result = json.loads(text)
        if not isinstance(result, dict):
            raise ValueError('JSON object required')
        return result


def search(query):
    url = 'https://news.google.com/rss/search?' + urlencode({'q': query, 'hl': 'ja', 'gl': 'JP', 'ceid': 'JP:ja'})
    request = urllib.request.Request(url, headers={'User-Agent': 'news-now/1.0'})
    with urllib.request.urlopen(request, timeout=30) as response:
        root = ET.fromstring(response.read())
    return [{'title': item.findtext('title', ''), 'url': item.findtext('link', ''),
             'published': item.findtext('pubDate', '')} for item in root.findall('.//item')][:20]
