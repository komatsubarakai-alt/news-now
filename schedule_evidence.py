"""Apply reviewed date evidence only to the unchanged, named stored schedule."""
import copy
from datetime import date
from pathlib import Path
import json


def apply_verified_dates(events, now, path='verified_schedule_dates.json'):
    records = json.loads(Path(path).read_text()) if Path(path).exists() else []
    for record in records:
        start, end = record.get('due_start'), record.get('due_end')
        if not record.get('source_quote') or not record.get('source_url', '').startswith('https://'):
            continue
        try:
            if date.fromisoformat(start) > date.fromisoformat(end):
                continue
        except (ValueError, TypeError):
            continue
        event = next((e for e in events if e.get('event_id') == record['event_id']), None)
        if not event or event.get('publication_status') == 'held':
            continue
        for schedule in event.get('schedules', []):
            if (schedule.get('schedule_id') != record['schedule_id'] or
                schedule.get('date_text') != record['expected_date_text'] or
                schedule.get('status') != 'scheduled' or
                schedule.get('precision') not in (None, 'unknown')):
                continue
            schedule.setdefault('history', []).append(copy.deepcopy({
                k: schedule.get(k) for k in ('date_text', 'due_start', 'due_end', 'precision', 'status', 'source_url')}))
            schedule.update(due_start=start, due_end=end, precision='day',
                date_confirmation={'source_url': record['source_url'], 'quote': record['source_quote'],
                    'year_context': record['year_context'], 'reviewed_at': record['reviewed_at'],
                    'applied_at': now.isoformat()})

