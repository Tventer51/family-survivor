import csv
import io
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
from shiny import App, Inputs, Outputs, Session, reactive, render, ui, req
import game

game.initialize()
LOCAL = ZoneInfo('America/New_York')
BOOTSTRAP_TOKEN = os.environ.get('SURVIVOR_SETUP_TOKEN','')
TEAM_NAMES = sorted(t['name'] for t in game.SEED['teams'])
NAMES = sorted(game.SEED['contestants'])
RULES = {str(i):r for i,r in enumerate(game.SEED['rules'])}

def display_time(stamp):
    return datetime.fromtimestamp(stamp,LOCAL).strftime('%b %d, %Y · %I:%M %p %Z') if stamp else 'Not scheduled'

def parse_deadline(value):
    if not value.strip(): return None
    try:
        dt=datetime.fromisoformat(value.strip())
        if dt.tzinfo is None: raise ValueError()
        return dt.timestamp()
    except ValueError: raise game.GameError('Use a date and time with UTC offset, e.g. 2026-10-14T20:00:00-04:00.')

def frame(records, columns=None):
    return pd.DataFrame(records,columns=columns) if columns else pd.DataFrame(records)

def remaining_names(records, episodes, through_episode=None):
    eliminated={name for episode in episodes if episode['results'] and
                (through_episode is None or episode['number']<=through_episode)
                for name in json.loads(episode['out_names'])}
    return [{**row,'Contestants remaining':', '.join(name for name in row['Roster'].split(', ')
             if name not in eliminated) or 'None'} for row in records]

app_ui = ui.page_fluid(
    ui.tags.head(ui.tags.link(rel='stylesheet',href='style.css?v=grid3'),
                 ui.tags.link(rel='icon',href='favicon.svg')),
    ui.tags.header(ui.tags.div('SURVIVOR',class_='wordmark'),ui.tags.div('Pretty · Venter family / Season 51',class_='season-label')),
    ui.output_ui('surface'),
    title='Family Survivor · Season 51',
)

def server(input: Inputs, output: Outputs, session: Session):
    identity=reactive.value(None)
    revision=reactive.value(0)
    notice=reactive.value('')
    login_error=reactive.value('')
    selection_ready=reactive.value('')
    grid_loaded={}
    dropdown_choices={}
    pending_delete=reactive.value(None)
    state_data=reactive.value(None)

    def actor():
        ident=identity.get()
        req(ident)
        try: game.who(ident)
        except game.GameError: req(False)
        return ident

    def perform(fn, message):
        try:
            fn(); notice.set(message); revision.set(revision.get()+1)
            ui.notification_show(message,type='message',duration=6)
        except game.GameError as e:
            ui.notification_show(str(e),type='error',duration=10)
        except Exception:
            import logging
            logging.exception('Game operation failed')
            ui.notification_show('Unable to save. Your changes have not been confirmed. Try again.',type='error',duration=10)

    @reactive.effect
    def poll_state():
        revision.get()
        reactive.invalidate_later(5)
        latest=game.snapshot(actor())
        with reactive.isolate(): previous=state_data.get()
        # Polling must not invalidate table renderers when nothing changed.
        if latest!=previous: state_data.set(latest)

    @reactive.calc
    def state():
        ident=actor()
        current=state_data.get()
        req(current)
        req(current['user']['username']==ident['username'] and current['user']['version']==ident['version'])
        return current

    @reactive.effect
    def expire_session():
        ident=identity.get()
        if ident:
            reactive.invalidate_later(30)
            try: game.who(ident)
            except game.GameError: identity.set(None); login_error.set('Session ended. Please sign in again.')

    @render.ui
    def surface():
        ident=identity.get()
        if not ident:
            if not game.has_admin():
                return ui.tags.main(ui.card(ui.h1('Set up your family game'),
                    ui.p('Create the organizer account. Participants will use accounts you create for them.'),
                    ui.p('Set SURVIVOR_SETUP_TOKEN on the server to enable setup.') if not BOOTSTRAP_TOKEN else None,
                    ui.input_password('setup_token','Private setup token'),
                    ui.input_text('setup_user','Organizer username'),ui.input_password('setup_password','Password · at least 12 characters'),
                    ui.input_action_button('setup','Create organizer account',class_='btn-primary'),ui.output_text('signin_message')),class_='login-wrap')
            return ui.tags.main(ui.card(ui.tags.div('YOUR WEEKLY PICKS',class_='eyebrow'),ui.h1('The tribe is waiting.'),
                ui.p('Sign in to choose your picks and follow the family standings.'),
                ui.input_text('username','Username'),ui.input_password('password','Password'),
                ui.input_action_button('login','Sign in',class_='btn-primary'),ui.output_text('signin_message'),
                ui.tags.small('Need an account or a password reset? Contact your game organizer.')),class_='login-wrap')
        try: user=game.who(ident)
        except game.GameError: return ui.p('Session ended. Please sign in again.')
        tabs=[ui.nav_panel('Cast picks',
                ui.layout_columns(ui.card(ui.card_header('Upcoming episode'),ui.output_ui('voting_form')),
                                  ui.card(ui.card_header('Your team'),ui.output_ui('team_card')),col_widths=(7,5))),
              ui.nav_panel('Standings',ui.output_ui('stats'),
                           ui.input_select('standings_episode','View standings',{'0':'Current standings',**{str(n):f'After episode {n}' for n in range(1,14)}}),
                           ui.input_radio_buttons('standings_mode','Ranking basis',{'cumulative':'Cumulative season score','weekly':'That episode only'},selected='cumulative',inline=True),
                           ui.output_text('standings_note'),ui.card(ui.output_data_frame('standings')),
                           ui.card(ui.card_header('Contestant points'),ui.output_data_frame('contestant_scores'))),
              ui.nav_panel('Pick history',ui.p('Your picks appear immediately. Everyone else’s weekly picks appear after results are published.'),
                           ui.output_data_frame('pick_history'),ui.output_ui('pick_delete_controls'),
                           ui.h3('Season-long predictions'),ui.output_data_frame('predictions_table')),
              ui.nav_panel('Rules',ui.output_ui('rules_display')),
              ui.nav_panel('My password',ui.card(ui.input_password('old_password','Current password'),
                           ui.input_password('new_password','New password · at least 12 characters'),
                           ui.input_action_button('change_password','Change password',class_='btn-primary')))]
        if user['admin']:
            tabs.append(ui.nav_panel('Organizer',
                ui.navset_card_tab(
                  ui.nav_panel('Episodes',ui.p('1. Schedule and open voting. 2. After the deadline, add scoring entries. 3. Confirm the outcomes and publish results.'),
                    ui.input_select('admin_episode','Episode',{str(n):f'Episode {n}' for n in range(1,14)},selected='4'),
                    ui.output_ui('episode_editor'),ui.output_data_frame('episode_table')),
                  ui.nav_panel('Scoring',ui.p('Check the events each contestant earned, then save the whole episode grid. Uncheck an event to remove it. Save before switching episodes. Points update when episode results are published.'),
                    ui.input_select('score_episode','Episode',{str(n):f'Episode {n}' for n in range(1,14)},selected='4'),
                    ui.output_ui('score_grid'),
                    ui.input_action_button('save_grid','Save episode grid',class_='btn-primary'),
                    ui.input_action_button('reload_grid','Reload saved grid'),
                    ui.p('Existing custom scoring entries remain in the table below. Use Adjustments for medical half-penalties or other exceptions; enter a reason.'),
                    ui.output_data_frame('event_table'),ui.input_numeric('delete_id','Entry ID to remove',0,min=0),
                    ui.input_action_button('delete_score','Remove entry'),
                    ui.hr(),ui.input_select('season_winner','Confirmed season winner',{'':'Not yet known',**{n:n for n in NAMES}}),
                    ui.input_action_button('save_winner','Record season winner')),
                  ui.nav_panel('Accounts',ui.h3('Create a participant'),
                    ui.input_text('account_username','New username'),ui.input_password('account_password','Initial password · at least 12 characters'),
                    ui.input_selectize('account_teams','Teams this person may manage',TEAM_NAMES,multiple=True),
                    ui.input_action_button('create_account','Create account',class_='btn-primary'),ui.hr(),ui.h3('Manage an existing account'),
                    ui.output_ui('account_editor'),ui.output_data_frame('accounts_table')),
                  ui.nav_panel('Import review & backups',ui.tags.div(*[ui.p(n) for n in game.SEED['notes']]),
                    ui.p('Weekly picks and season-long predictions were preserved. Scores start at zero and calculate only when the organizer enters and publishes episode results.'),
                    ui.h3('Correct an imported winner prediction'),ui.p('Use this only to correct the historical record. Every correction is logged.'),
                    ui.input_select('correction_team','Team to correct',TEAM_NAMES),
                    ui.input_select('correction_winner','Original winner prediction',{'':'No prediction (clear existing)',**{n:n for n in NAMES}}),
                    ui.input_numeric('correction_episode','Episode originally submitted',1,min=1,max=13),
                    ui.input_text('correction_reason','Reason / source for correction'),
                    ui.input_action_button('correct_prediction','Save historical correction'),ui.hr(),
                    ui.download_button('download_scores','Download scoreboard CSV'),ui.download_button('download_backup','Download full database backup'),
                    ui.h3('Audit history'),ui.output_data_frame('audit_table')))))
        return ui.tags.main(ui.tags.div(ui.tags.span(f"Signed in as {user['username']}"),ui.input_action_button('logout','Sign out'),class_='account-strip'),
                            ui.navset_tab(*tabs,id='section'),class_='game-wrap')

    @render.text
    def signin_message(): return login_error.get()

    @reactive.effect
    @reactive.event(input.setup)
    def setup():
        try:
            if not BOOTSTRAP_TOKEN or not secrets.compare_digest(input.setup_token(),BOOTSTRAP_TOKEN):
                raise game.GameError('The setup token is incorrect or setup is disabled.')
            game.bootstrap(input.setup_user(),input.setup_password())
            identity.set(game.login(input.setup_user(),input.setup_password())); login_error.set('')
        except game.GameError as e: login_error.set(str(e))

    @reactive.effect
    @reactive.event(input.login)
    def login():
        try:
            identity.set(game.login(input.username(),input.password())); login_error.set('')
            ui.update_text('username',value=''); ui.update_text('password',value='')
        except game.GameError as e: login_error.set(str(e))

    @reactive.effect
    @reactive.event(input.logout)
    def logout(): identity.set(None); selection_ready.set(''); dropdown_choices.clear()

    @render.ui
    def voting_form():
        s=game.snapshot(actor()); own=game.my_teams(actor())
        if not own: return ui.p('Your organizer needs to assign a team to your account before you can cast picks.')
        upcoming=next((e['number'] for e in s['episodes'] if not e['results'] and not e['closed']),
                       next((e['number'] for e in s['episodes'] if not e['results'] and e['number']>=game.game_start_episode()),13))
        # Keep these inputs stable across polling and saves.
        return ui.TagList(ui.input_select('vote_team','Team',{t:t for t in own}),
            ui.input_select('vote_episode','Episode',{str(e['number']):f"Episode {e['number']}" for e in s['episodes'] if e['number']>=4},selected=str(upcoming)),
            ui.output_ui('pick_fields'),ui.output_text('save_status'))

    @render.ui
    def pick_fields():
        ident=actor(); team=input.vote_team(); ep=int(input.vote_episode())
        s=game.snapshot(ident)
        episode=next(e for e in s['episodes'] if e['number']==ep)
        pick=next((p for p in s['picks'] if p['team']==team and p['episode']==ep),{})
        prediction=next((p for p in s['predictions'] if p['team']==team),None)
        with game.connection() as db: names=game.eligible(db,ep)
        opened=not episode['closed'] and not episode['results'] and episode['deadline'] and time.time()<episode['deadline']
        label='Voting open' if opened else 'Voting closed'
        fields=[ui.tags.div(ui.output_text('vote_window'),class_='vote-status'),ui.p(f"Deadline: {display_time(episode['deadline'])}"),
                ui.input_selectize('pick_out','Who goes home?',{'':'Choose a contestant',**{n:n for n in names}},selected=pick.get('out_name','')),
                ui.input_selectize('pick_title','Who says the episode title?',{'':'Choose a speaker',**{n:n for n in names},'Jeff Probst':'Jeff Probst','No One':'No One'},selected=pick.get('title',''))]
        if prediction and prediction['episode']!=ep:
            fields.append(ui.p(f"Sole Survivor: {prediction['contestant']} · locked in episode {prediction['episode']} · {game.SEED['bonuses'][str(prediction['episode'])]} points if correct."))
        else:
            fields.extend([ui.input_selectize('pick_winner',f"Season-long winner · {game.SEED['bonuses'][str(ep)]} points if correct (optional)",
                          {'':'Decide later',**{n:n for n in names}},selected=prediction['contestant'] if prediction else ''),
                           ui.tags.small('You can change your winner within this episode’s voting window. It locks when that window closes.')])
        fields.append(ui.input_action_button('save_pick','Save picks',class_='btn-primary',disabled=not opened))
        fields.append(ui.tags.small('You can update weekly picks until the deadline. Saving again replaces your previous choices.'))
        return ui.TagList(*fields)

    @render.text
    def save_status(): return notice.get()

    @render.text
    def vote_window():
        s=state(); ep=int(input.vote_episode())
        e=next(e for e in s['episodes'] if e['number']==ep)
        opened=not e['closed'] and not e['results'] and e['deadline'] and time.time()<e['deadline']
        ui.update_action_button('save_pick',disabled=not opened)
        return 'Voting open · closes Wednesday at 8 p.m. Eastern' if opened else 'Voting closed'

    @reactive.effect
    @reactive.event(input.save_pick)
    def save_pick():
        def save():
            ident=actor(); team=input.vote_team(); ep=int(input.vote_episode())
            # The client cannot select another account's team through forged inputs.
            p=next((p for p in game.snapshot(ident)['predictions'] if p['team']==team),None)
            winner=input.pick_winner() if not p or p['episode']==ep else ''
            game.save_pick(ident,team,ep,input.pick_out(),input.pick_title(),winner)
        perform(save,'Your picks were saved.')

    @render.ui
    def team_card():
        own=game.my_teams(actor())
        if not own: return ui.p('This organizer account has no assigned voting team.')
        s=state(); team=input.vote_team()
        if team not in own: return ui.p('Choose an assigned team.')
        row=next((r for r in s['standings'] if r['Team']==team),None)
        if not row: return ui.p('Choose an assigned team.')
        return ui.TagList(ui.h2(team),ui.tags.div(f"{row['Total']:g}",class_='big-score'),ui.p(f"Rank {row['Rank']} · {row['Remaining']} contestants remaining"),
                          ui.p(row['Roster']),ui.hr(),ui.p(f"Draft: {row['Draft points']:g} · Picks: {row['Weekly picks']:g} · Winner: {row['Winner bonus']:g}"))

    @render.ui
    def stats():
        s=state(); ep=int(input.standings_episode())
        published=sum(e['results'] and (not ep or e['number']<=ep) for e in s['episodes'])
        rankings=game.weekly_standings(actor(),ep,input.standings_mode()=='weekly') if ep else s['standings']
        return ui.tags.div(ui.tags.div(ui.h2(str(len(s['teams']))),ui.p('family teams')),
                           ui.tags.div(ui.h2(str(published)),ui.p('episodes scored')),
                           ui.tags.div(ui.h2(rankings[0]['Team']),ui.p('leading this view')),class_='stats')

    @render.text
    def standings_note():
        s=state(); ep=int(input.standings_episode())
        if not ep: return 'Current published scores. Select an earlier episode to review weekly rankings.'
        e=next(e for e in s['episodes'] if e['number']==ep)
        return f'Episode {ep}: rankings reflect the latest corrected results.' if e['results'] else f'Episode {ep} results are not published yet.'

    @render.data_frame
    def standings():
        s=state(); ep=int(input.standings_episode())
        if not ep: return render.DataGrid(frame(remaining_names(s['standings'],s['episodes']),['Rank','Team','Total','Draft points','Weekly picks','Winner bonus','Contestants remaining','Roster']))
        rows=game.weekly_standings(actor(),ep,input.standings_mode()=='weekly')
        return render.DataGrid(frame(remaining_names(rows,s['episodes'],ep),['Rank','Team','Week total','Week draft','Week picks','Week bonus','Cumulative','Contestants remaining']))

    @render.data_frame
    def contestant_scores():
        state(); ep=int(input.standings_episode())
        s=game.snapshot(actor(),ep or None)
        return render.DataGrid(frame([{'Contestant':n,'Points':p} for n,p in sorted(s['contestant_points'].items(),key=lambda x:-x[1])]))

    @render.data_frame
    def pick_history():
        revision.get(); s=game.snapshot(actor())
        records=[{'Episode':p['episode'],'Team':p['team'],'Out pick':p['out_name'],'Title pick':p['title'],'Pick points':p['points'],
                  'Saved':display_time(p['updated']) if p['updated'] else 'Imported'} for p in s['picks']]
        return render.DataGrid(frame(records,['Episode','Team','Out pick','Title pick','Pick points','Saved']),filters=True,selection_mode='row')

    @render.ui
    def pick_delete_controls():
        if not game.who(actor())['admin']: return None
        revision.get()
        deleted=game.rows('SELECT id,team,episode FROM deleted_picks ORDER BY id DESC')
        return ui.card(ui.h4('Organizer: remove a weekly pick'),ui.p('Select one row in the table above. Deletion removes its weekly points but keeps the season-long prediction. You can restore the row below.'),
          ui.input_text('delete_pick_reason','Reason for deletion'),ui.input_action_button('request_delete_pick','Delete selected pick'),
          ui.input_select('restore_pick_id','Deleted pick to restore',{'':'Choose a deleted pick',**{str(r['id']):f"{r['team']} · episode {r['episode']}" for r in deleted}}),
          ui.input_action_button('restore_pick','Restore deleted pick'))

    @reactive.effect
    @reactive.event(input.request_delete_pick)
    def request_delete_pick():
        organizer()
        selected=pick_history.data_view(selected=True)
        if len(selected)!=1:
            ui.notification_show('Select one pick-history row first.',type='warning'); return
        record=selected.iloc[0]
        pending_delete.set({'team':str(record['Team']),'episode':int(record['Episode'])})
        ui.modal_show(ui.modal(ui.p(f"Delete {record['Team']}'s episode {int(record['Episode'])} weekly pick? Its points will be removed. The row remains recoverable."),
          footer=ui.TagList(ui.modal_button('Cancel'),ui.input_action_button('confirm_delete_pick','Delete this pick',class_='btn-danger')),title='Delete weekly pick'))

    @reactive.effect
    @reactive.event(input.confirm_delete_pick)
    def confirm_delete_pick():
        record=pending_delete.get()
        if not record: ui.modal_remove(); return
        perform(lambda:game.delete_pick(organizer(),record['team'],record['episode'],input.delete_pick_reason()),'Weekly pick removed. You can restore it below.')
        pending_delete.set(None)
        ui.modal_remove()

    @reactive.effect
    @reactive.event(input.restore_pick)
    def restore_pick():
        def restore():
            if not input.restore_pick_id(): raise game.GameError('Choose a deleted pick.')
            game.restore_pick(organizer(),int(input.restore_pick_id()))
        perform(restore,'Weekly pick restored; scores recalculated.')

    @render.data_frame
    def predictions_table():
        return render.DataGrid(frame([{'Team':p['team'],'Winner prediction':p['contestant'],'Episode locked':p['episode'],
                'Bonus if correct':game.SEED['bonuses'][str(p['episode'])]} for p in state()['predictions']],['Team','Winner prediction','Episode locked','Bonus if correct']))

    @render.ui
    def rules_display():
        actor()
        return ui.TagList(ui.h2('How your team scores'),ui.p('Drafted contestants’ points + weekly prediction points + one season-long winner bonus.'),
          ui.p('Correct out pick: 30 points. Correct episode-title speaker (including No One): 30 points. Incorrect picks: 0 points.'),
          ui.p('Sole Survivor: one prediction per team, locked after the episode you submit it. Bonus: '+', '.join(f"Ep {n}: {p:g}" for n,p in game.SEED['bonuses'].items())+'.'),
          ui.p('A challenge that awards both reward and immunity is scored only as immunity. Medical evacuation receives half the applicable elimination penalty. Tied second-place finishers receive 60 points each.'),
          *[ui.card(ui.h4(f"{r['section']} / {r['group']}"),ui.p(f"{r['label']}: {r['points']} points"),ui.tags.small(r['note'])) for r in game.SEED['rules']])

    @reactive.effect
    @reactive.event(input.change_password)
    def password_change():
        def change():
            game.change_password(actor(),input.old_password(),input.new_password()); identity.set(None)
        perform(change,'Password changed. Sign in with your new password.')

    def organizer():
        ident=actor()
        req(game.who(ident)['admin'])
        with game.connection() as db: game.require(db,ident,admin=True)
        return ident

    def eligible_names(ep):
        with game.connection() as db: return game.eligible(db,ep)

    @render.ui
    def score_grid():
        ident=organizer(); revision.get(); ep=int(input.score_episode())
        data=game.scoring_grid(ident,ep)
        grid_loaded.clear(); grid_loaded.update({'episode':ep,**data})
        groups={}
        for key,rule in game.GRID_RULES.items():
            group=rule['section'] if rule['section']!='GAMEPLAY' else rule['group']
            groups.setdefault(group,[]).append((key,rule))
        panels=[]
        for group,rules in groups.items():
            headings=[ui.tags.th('Contestant',class_='sticky-name')]+[ui.tags.th(f"{r['group']} · {r['label']} ({r['points']:+g})",title=r['note']) for k,r in rules]
            body=[]
            for name in data['names']:
                idx=NAMES.index(name); cells=[ui.tags.th(name,class_='sticky-name',scope='row')]
                for key,rule in rules:
                    matches=[e for e in data['events'] if e['contestant']==name and e['category']==rule['label'] and e['points']==rule['points']]
                    label=ui.tags.span(f"{name}: {rule['group']} {rule['label']} {rule['points']:+g}",class_='visually-hidden')
                    cells.append(ui.tags.td(ui.input_checkbox(f'g{idx}_{key}',label,value=bool(matches)),ui.tags.small(f'{len(matches)} entries kept') if len(matches)>1 else None))
                body.append(ui.tags.tr(*cells))
            table=ui.tags.div(ui.tags.table(ui.tags.thead(ui.tags.tr(*headings)),ui.tags.tbody(*body),class_='scoring-matrix'),class_='matrix-scroll')
            panels.append(ui.nav_panel(group.title(),table))
        adjustment_rows=[]
        for name in data['names']:
            idx=NAMES.index(name); events=[e for e in data['events'] if e['contestant']==name and e['category']=='Grid adjustment']
            adjustment_rows.append(ui.tags.tr(ui.tags.th(name,scope='row'),ui.tags.td(ui.input_numeric(f'adj{idx}',ui.tags.span(f'{name} adjustment',class_='visually-hidden'),sum(e['points'] for e in events),min=-1000,max=1000)),
              ui.tags.td(ui.input_text(f'why{idx}',ui.tags.span(f'{name} adjustment reason',class_='visually-hidden'),value='; '.join(e['note'] for e in events)))))
        panels.append(ui.nav_panel('Adjustments',ui.tags.table(ui.tags.thead(ui.tags.tr(ui.tags.th('Contestant'),ui.tags.th('Extra / deducted points'),ui.tags.th('Reason'))),ui.tags.tbody(*adjustment_rows),class_='scoring-matrix')))
        return ui.navset_card_tab(*panels)

    @reactive.effect
    @reactive.event(input.reload_grid)
    def reload_grid(): organizer(); revision.set(revision.get()+1)

    @reactive.effect
    @reactive.event(input.save_grid)
    def save_grid():
        def save():
            ident=organizer(); ep=int(input.score_episode())
            if grid_loaded.get('episode')!=ep: raise game.GameError('Wait for the selected episode grid to load.')
            selected={n:[key for key in game.GRID_RULES if input[f'g{NAMES.index(n)}_{key}']()] for n in grid_loaded['names']}
            adjustments={n:(input[f'adj{NAMES.index(n)}'](),input[f'why{NAMES.index(n)}']()) for n in grid_loaded['names']}
            game.save_scoring_grid(ident,ep,selected,adjustments,grid_loaded['version'])
        perform(save,'Episode grid saved. Publish results to include these points in standings.')

    @reactive.effect
    def filter_episode_contestants():
        ident=organizer(); state()
        ep=int(input.admin_episode()); names=eligible_names(ep)
        key=(ident['username'],ep,tuple(names))
        if dropdown_choices.get('organizer')==key: return
        dropdown_choices['organizer']=key
        with reactive.isolate():
            selected=[n for n in (input.episode_out() or []) if n in names]
            title=input.episode_title()
        ui.update_selectize('episode_out',choices=names,selected=selected)
        choices={'':'Not confirmed',**{n:n for n in names},'Jeff Probst':'Jeff Probst','No One':'No One'}
        ui.update_select('episode_title',choices=choices,selected=title if title in choices else '')

    @reactive.effect
    def filter_voting_contestants():
        ident=actor(); state()
        ep=int(input.vote_episode()); names=eligible_names(ep)
        key=(ident['username'],input.vote_team(),ep,tuple(names))
        if dropdown_choices.get('voting')==key: return
        dropdown_choices['voting']=key
        for control,choices in [('pick_out',{'':'Choose a contestant',**{n:n for n in names}}),
                                ('pick_title',{'':'Choose a speaker',**{n:n for n in names},'Jeff Probst':'Jeff Probst','No One':'No One'})]:
            with reactive.isolate(): selected=input[control]()
            ui.update_selectize(control,choices=choices,selected=selected if selected in choices else '')

    @render.ui
    def episode_editor():
        ident=organizer(); ep=int(input.admin_episode())
        e=next(e for e in game.snapshot(ident)['episodes'] if e['number']==ep)
        names=eligible_names(ep)
        return ui.TagList(ui.p(f"Automatic deadline: {display_time(e['deadline'])}"),
          ui.p('Voting closes automatically every Wednesday at 8 p.m. Eastern. Daylight saving time is handled automatically.'),
          ui.input_checkbox('episode_closed','Voting closed',value=bool(e['closed'])),
          ui.input_selectize('episode_out','Contestants eliminated (supports multiple)',names,selected=json.loads(e['out_names']),multiple=True),
          ui.input_select('episode_title','Actual episode-title speaker',{'':'Not confirmed',**{n:n for n in names},'Jeff Probst':'Jeff Probst','No One':'No One'},selected=e['title']),
          ui.input_checkbox('episode_results','Publish episode results and calculate scores',value=bool(e['results'])),
          ui.p('Enter the actual episode outcomes. Publishing calculates preserved weekly picks automatically: 30 points for a correct out pick and 30 for the title speaker. Add contestant scoring entries under Scoring to calculate drafted-team points.'),
          ui.input_action_button('save_episode','Save episode',class_='btn-primary'))

    @reactive.effect
    @reactive.event(input.save_episode)
    def save_episode():
        perform(lambda:game.set_episode(organizer(),int(input.admin_episode()),None,
                 input.episode_closed(),list(input.episode_out()),input.episode_title(),input.episode_results()),'Episode saved.')

    @render.data_frame
    def episode_table():
        organizer()
        return render.DataGrid(frame([{'Episode':e['number'],'Deadline':display_time(e['deadline']),'Closed':bool(e['closed']),
          'Results published':bool(e['results']),'Out':', '.join(json.loads(e['out_names'])),'Title speaker':e['title'],
          'Outcomes confirmed':bool(e['confirmed'])} for e in state()['episodes']]))

    @render.data_frame
    def event_table():
        organizer()
        return render.DataGrid(frame([e for e in state()['events'] if e['episode']==int(input.score_episode())],['id','episode','contestant','category','points','note']))

    @reactive.effect
    @reactive.event(input.delete_score)
    def delete_score(): perform(lambda:game.delete_event(organizer(),int(input.delete_id())),'Scoring entry removed; audit history retained.')

    @reactive.effect
    @reactive.event(input.save_winner)
    def save_winner(): perform(lambda:game.set_winner(organizer(),input.season_winner()),'Season winner saved; bonuses recalculated.')

    @reactive.effect
    @reactive.event(input.create_account)
    def create_account():
        perform(lambda:game.create_user(organizer(),input.account_username(),input.account_password(),list(input.account_teams())),'Participant account created.')
        ui.update_text('account_password',value='')

    @render.ui
    def account_editor():
        organizer(); revision.get()
        users=game.rows('SELECT username FROM users WHERE admin=0 ORDER BY username')
        if not users: return ui.p('Create an account first.')
        return ui.TagList(ui.input_select('manage_username','Participant',{u['username']:u['username'] for u in users}),ui.output_ui('manage_fields'))

    @render.ui
    def manage_fields():
        organizer(); username=input.manage_username()
        user=game.rows('SELECT active FROM users WHERE username=?',(username,))[0]
        assigned=[r['team'] for r in game.rows('SELECT team FROM access WHERE username=?',(username,))]
        return ui.TagList(ui.input_selectize('manage_teams','Assigned teams',TEAM_NAMES,selected=assigned,multiple=True),
          ui.input_checkbox('manage_active','Account enabled',value=bool(user['active'])),
          ui.input_password('manage_password','Reset password (leave blank to keep current password)'),
          ui.input_action_button('manage_save','Update account'))

    @reactive.effect
    @reactive.event(input.manage_save)
    def manage_save(): perform(lambda:game.manage_user(organizer(),input.manage_username(),list(input.manage_teams()),input.manage_password(),input.manage_active()),'Account updated. Existing sessions for this participant are revoked.')

    @render.data_frame
    def accounts_table():
        ident=organizer(); revision.get()
        return render.DataGrid(frame(game.accounts(ident)))

    @render.data_frame
    def audit_table():
        organizer(); revision.get()
        return render.DataGrid(frame([{**r,'at':display_time(r['at'])} for r in game.rows('SELECT actor,action,details,at FROM audit ORDER BY id DESC LIMIT 200')]))

    @render.download(filename='survivor-s51-standings.csv')
    def download_scores():
        organizer(); s=game.snapshot(actor()); buf=io.StringIO()
        writer=csv.DictWriter(buf,fieldnames=['Rank','Team','Total','Draft points','Weekly picks','Winner bonus','Remaining','Contestants remaining','Roster'])
        writer.writeheader(); writer.writerows(remaining_names(s['standings'],s['episodes'])); yield buf.getvalue()

    @reactive.effect
    @reactive.event(input.correct_prediction)
    def correct_prediction():
        perform(lambda:game.correct_prediction(organizer(),input.correction_team(),input.correction_winner(),
                    int(input.correction_episode()),input.correction_reason()),'Historical prediction corrected; audit history retained.')

    @render.download(filename='survivor-s51-backup.sqlite3')
    def download_backup():
        ident=organizer()
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'backup.sqlite3'; game.backup(ident,path); yield path.read_bytes()

app=App(app_ui,server,static_assets=Path(__file__).parent/'www')
