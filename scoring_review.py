"""Read-only, published episode scoring summaries."""
import json

COLUMNS=['Contestant','Reward','Immunity','Idols / advantages','SITD','Vote side','Elimination','Other','Total']

def category(event,rules):
    label=event['category']; lower=label.lower()
    if label in ('Being on the right side of the vote','Being on the wrong side of the vote'): return 'Vote side'
    rule=next((r for r in rules if r['label']==label),None)
    if not rule: return 'Other'
    if rule['section']=='CHALLENGES':
        if 'reward' in lower: return 'Reward'
        if 'immunity' in lower: return 'Immunity'
    if rule['group']=='Shot in the Dark (SITD)': return 'SITD'
    if rule['group']=='Idol/Advantage': return 'Idols / advantages'
    if rule['group']=='Getting voted out' and event['points']<0: return 'Elimination'
    return 'Other'

def summary(snapshot,episode,rules,contestants):
    outcome=next(e for e in snapshot['episodes'] if e['number']==episode)
    if not outcome['results']: return []
    eliminated={n for e in snapshot['episodes'] if e['number']<episode and e['results'] for n in json.loads(e['out_names'])}
    events=[e for e in snapshot['events'] if e['episode']==episode]
    names=(set(contestants)-eliminated)|{e['contestant'] for e in events}
    rows={n:{'Contestant':n,**{c:0 for c in COLUMNS[1:]}} for n in sorted(names)}
    for event in events:
        row=rows[event['contestant']]
        row[category(event,rules)]+=event['points']; row['Total']+=event['points']
    return list(rows.values())
