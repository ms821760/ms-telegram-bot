import os
import json
import requests
import schedule
import time
import threading
from datetime import date, datetime, timedelta
from flask import Flask, request, jsonify

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
SUPABASE_URL    = os.environ.get('SUPABASE_URL')
SUPABASE_KEY    = os.environ.get('SUPABASE_KEY')
ANTHROPIC_KEY   = os.environ.get('ANTHROPIC_API_KEY')

TELEGRAM_API    = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}'

SB_HEADERS = {
    'apikey':        SUPABASE_KEY,
    'Authorization': f'Bearer {SUPABASE_KEY}',
    'Content-Type':  'application/json',
    'Prefer':        'resolution=merge-duplicates'
}

TODAY = lambda: date.today().isoformat()

# ── Conversation state (in-memory) ───────────────────────────
# Tracks what check-in step the user is on
state = {
    'mode': None,        # 'morning', 'afternoon', 'evening', 'question'
    'step': None,        # which question we're on
    'data': {}           # collected answers
}

# ── Telegram helpers ──────────────────────────────────────────
def send_message(text, reply_markup=None):
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text':    text,
        'parse_mode': 'Markdown'
    }
    if reply_markup:
        payload['reply_markup'] = json.dumps(reply_markup)
    r = requests.post(f'{TELEGRAM_API}/sendMessage', json=payload)
    return r.json()

def send_quick_replies(text, options):
    keyboard = {
        'keyboard': [[{'text': str(o)} for o in options]],
        'one_time_keyboard': True,
        'resize_keyboard': True
    }
    send_message(text, reply_markup=keyboard)

def remove_keyboard(text):
    send_message(text, reply_markup={'remove_keyboard': True})

# ── Supabase helpers ──────────────────────────────────────────
def run_query(sql):
    r = requests.post(
        f'{SUPABASE_URL}/rest/v1/rpc/run_query',
        headers=SB_HEADERS,
        json={'query_text': sql},
        timeout=15
    )
    r.raise_for_status()
    return r.json()

def upsert(table, row, on_conflict):
    r = requests.post(
        f'{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}',
        headers=SB_HEADERS,
        json=[row]
    )
    r.raise_for_status()
    return r

def insert(table, row):
    r = requests.post(
        f'{SUPABASE_URL}/rest/v1/{table}',
        headers={**SB_HEADERS, 'Prefer': 'return=minimal'},
        json=[row]
    )
    r.raise_for_status()

# ── Claude helper ─────────────────────────────────────────────
def ask_claude(prompt, max_tokens=500):
    r = requests.post(
        'https://api.anthropic.com/v1/messages',
        headers={
            'Content-Type':  'application/json',
            'x-api-key':     ANTHROPIC_KEY,
            'anthropic-version': '2023-06-01'
        },
        json={
            'model':      'claude-sonnet-4-20250514',
            'max_tokens': max_tokens,
            'messages':   [{'role': 'user', 'content': prompt}]
        },
        timeout=30
    )
    r.raise_for_status()
    return ''.join(b.get('text', '') for b in r.json().get('content', []))

# ── Data fetchers ─────────────────────────────────────────────
def get_recent_training():
    return run_query("""
        SELECT date, run_min, ride_min, strength_min, cardio_min,
               z1_min, z2_min, z3_min, z4_min, z5_min,
               total_calories_kcal, steps
        FROM daily_activity_summary
        WHERE date >= CURRENT_DATE - INTERVAL '7 days'
        ORDER BY date DESC
        LIMIT 7
    """)

def get_health_metrics():
    return run_query("""
        SELECT date, resting_hr_bpm, hrv_ms, steps, active_calories_kcal
        FROM daily_health
        WHERE date >= CURRENT_DATE - INTERVAL '7 days'
        ORDER BY date DESC
        LIMIT 7
    """)

def get_training_load():
    return run_query("""
        SELECT date, run_min, ride_min, strength_min, cardio_min,
               z1_min, z2_min, z3_min, z4_min, z5_min
        FROM daily_activity_summary
        WHERE date >= CURRENT_DATE - INTERVAL '42 days'
        ORDER BY date
    """)

def get_nutrition_recent():
    return run_query("""
        SELECT date, calories_kcal, protein_g, carbs_g, fat_g
        FROM daily_nutrition
        WHERE date >= CURRENT_DATE - INTERVAL '3 days'
        ORDER BY date DESC
    """)

def get_mental_health_recent():
    return run_query("""
        SELECT date, time_of_day, energy, mood, stress, sleep_quality, notes
        FROM mental_health_logs
        WHERE date >= CURRENT_DATE - INTERVAL '3 days'
        ORDER BY logged_at DESC
        LIMIT 10
    """)

def get_body_comp():
    return run_query("""
        SELECT date, weight_lb, body_fat_pct, skeletal_muscle_mass_lb, inbody_score
        FROM body_composition
        ORDER BY date DESC
        LIMIT 1
    """)

def check_nutrition_logged_today():
    result = run_query(f"""
        SELECT COUNT(*) as cnt FROM daily_nutrition
        WHERE date = '{TODAY()}'
    """)
    return result[0]['cnt'] > 0 if result else False

# ── ATL/CTL calculation ───────────────────────────────────────
def calculate_tsb(activity_data):
    IF = {'z1': 0.55, 'z2': 0.72, 'z3': 0.87, 'z4': 0.98, 'z5': 1.10}

    def calc_tss(row):
        total = 0
        for z, factor in IF.items():
            mins = row.get(f'{z}_min', 0) or 0
            total += (mins / 60) * (factor ** 2) * 100
        if total == 0:
            total_min = sum(row.get(f, 0) or 0 for f in
                          ['run_min','ride_min','strength_min','cardio_min'])
            total = (total_min / 60) * (0.65 ** 2) * 100
        return total

    start = date.today() - timedelta(days=41)
    tss_by_date = {r['date']: calc_tss(r) for r in (activity_data or [])}

    atl = ctl = 0.0
    atl_decay = 1 - (1/7)
    ctl_decay = 1 - (1/42)

    d = start
    while d <= date.today():
        tss = tss_by_date.get(d.isoformat(), 0)
        atl = atl * atl_decay + tss * (1 - atl_decay)
        ctl = ctl * ctl_decay + tss * (1 - ctl_decay)
        d += timedelta(days=1)

    return round(ctl, 1), round(atl, 1), round(ctl - atl, 1)

# ── Evening briefing ──────────────────────────────────────────
def send_evening_briefing():
    print('Sending evening briefing...')
    try:
        training    = get_recent_training()
        health      = get_health_metrics()
        load_data   = get_training_load()
        nutrition   = get_nutrition_recent()
        mental      = get_mental_health_recent()
        body_comp   = get_body_comp()
        nutrition_logged = check_nutrition_logged_today()

        ctl, atl, tsb = calculate_tsb(load_data)

        prompt = f"""You are a performance coach for a 47-year-old drilling engineer on sabbatical.

ATHLETE PROFILE:
- Current phase: Body Comp + MS 150 (ride April 25-26)
- Goals: Body fat <15%, muscle 105-110 lbs
- History: Overtraining prone, needs structured progression
- HR Zones: Z1<130, Z2 131-150, Z3 151-160, Z4 161-170, Z5>171
- Today: {TODAY()}

CURRENT FITNESS STATE:
- CTL (fitness): {ctl} | ATL (fatigue): {atl} | TSB (form): {tsb}
- TSB interpretation: {
    'Very fresh — ready to perform' if tsb > 10 else
    'Fresh — good to train hard' if tsb > 5 else
    'Neutral' if tsb > -10 else
    'Productive training load' if tsb > -25 else
    'High fatigue — consider recovery'
}

LAST 7 DAYS TRAINING:
{json.dumps(training, default=str)}

HEALTH METRICS (last 7 days):
{json.dumps(health, default=str)}

RECENT NUTRITION (last 3 days):
{json.dumps(nutrition, default=str)}
Nutrition logged today: {nutrition_logged}

RECENT MENTAL HEALTH CHECK-INS:
{json.dumps(mental, default=str)}

LATEST BODY COMP:
{json.dumps(body_comp, default=str)}

Generate a concise evening briefing for tomorrow. Format exactly like this:

*Evening Briefing — {date.today().strftime('%A, %B %d')}*

*Recovery Status:* [1-2 sentences on HRV, resting HR, TSB]

*Tomorrow's Recommendation:* [ONE of: Complete Rest / Active Recovery (easy walk) / Z2 Cardio / Z2 Ride / Strength — Upper / Strength — Lower / Tempo Run / Long Ride]

*Why:* [2-3 sentences explaining the recommendation based on data]

*Focus:* [1-2 specific actionable tips for tomorrow]

{f'⚠️ *Nutrition reminder:* Log your food today!' if not nutrition_logged else '✅ Nutrition logged today.'}

Keep it under 200 words. Be direct and specific."""

        briefing = ask_claude(prompt, max_tokens=400)
        send_message(briefing)
        print('Evening briefing sent.')

    except Exception as e:
        print(f'Evening briefing error: {e}')
        send_message(f'⚠️ Could not generate briefing: {str(e)[:100]}')

# ── Check-in flows ────────────────────────────────────────────
CHECKIN_FLOWS = {
    'morning': [
        ('sleep_quality', '🌅 *Morning check-in!*\n\nHow did you sleep last night?\n_(1 = terrible, 10 = perfect)_', [1,2,3,4,5,6,7,8,9,10]),
        ('energy',        'Energy level right now?\n_(1 = exhausted, 10 = great)_', [1,2,3,4,5,6,7,8,9,10]),
        ('stress',        'Stress level this morning?\n_(1 = very relaxed, 10 = very stressed)_', [1,2,3,4,5,6,7,8,9,10]),
        ('notes',         'Any notes? _(injuries, poor sleep reason, etc.)_ — or reply *skip*', None),
    ],
    'afternoon': [
        ('energy',  '☀️ *Afternoon check-in!*\n\nEnergy level right now?\n_(1 = exhausted, 10 = great)_', [1,2,3,4,5,6,7,8,9,10]),
        ('stress',  'Stress level?\n_(1 = very relaxed, 10 = very stressed)_', [1,2,3,4,5,6,7,8,9,10]),
        ('notes',   'Any notes? — or reply *skip*', None),
    ],
    'evening': [
        ('mood',    '🌙 *Evening check-in!*\n\nOverall mood today?\n_(1 = rough day, 10 = great day)_', [1,2,3,4,5,6,7,8,9,10]),
        ('energy',  'Energy level at end of day?\n_(1 = drained, 10 = still energized)_', [1,2,3,4,5,6,7,8,9,10]),
        ('stress',  'Stress level today overall?\n_(1 = very relaxed, 10 = very stressed)_', [1,2,3,4,5,6,7,8,9,10]),
        ('notes',   'Any notes about today? — or reply *skip*', None),
    ]
}

def start_checkin(time_of_day):
    state['mode'] = time_of_day
    state['step'] = 0
    state['data'] = {'time_of_day': time_of_day, 'date': TODAY()}
    ask_next_question()

def ask_next_question():
    flow = CHECKIN_FLOWS.get(state['mode'], [])
    step = state['step']
    if step >= len(flow):
        finish_checkin()
        return
    field, question, options = flow[step]
    if options:
        send_quick_replies(question, options)
    else:
        remove_keyboard(question)

def handle_checkin_response(text):
    flow = CHECKIN_FLOWS.get(state['mode'], [])
    step = state['step']
    if step >= len(flow):
        return

    field, question, options = flow[step]

    if options:
        try:
            val = int(text.strip())
            if val < 1 or val > 10:
                send_message('Please reply with a number between 1 and 10.')
                return
            state['data'][field] = val
        except ValueError:
            send_message('Please reply with a number between 1 and 10.')
            return
    else:
        state['data'][field] = None if text.strip().lower() == 'skip' else text.strip()

    state['step'] += 1
    ask_next_question()

def finish_checkin():
    try:
        row = {
            'date':          state['data'].get('date', TODAY()),
            'time_of_day':   state['data'].get('time_of_day'),
            'energy':        state['data'].get('energy'),
            'mood':          state['data'].get('mood'),
            'stress':        state['data'].get('stress'),
            'sleep_quality': state['data'].get('sleep_quality'),
            'notes':         state['data'].get('notes'),
            'logged_at':     datetime.utcnow().isoformat()
        }
        insert('mental_health_logs', row)
        remove_keyboard('✅ Check-in saved! Talk later.')
    except Exception as e:
        send_message(f'⚠️ Could not save check-in: {str(e)[:100]}')

    state['mode'] = None
    state['step'] = None
    state['data'] = {}

# ── Scheduled check-in triggers ───────────────────────────────
def trigger_morning_checkin():
    print('Triggering morning check-in...')
    start_checkin('morning')

def trigger_afternoon_checkin():
    print('Triggering afternoon check-in...')
    start_checkin('afternoon')

def trigger_evening_checkin():
    print('Triggering evening check-in...')
    start_checkin('evening')

def trigger_evening_briefing():
    send_evening_briefing()

# ── Quick question handler ────────────────────────────────────
def handle_question(question):
    try:
        training  = get_recent_training()
        health    = get_health_metrics()
        load_data = get_training_load()
        mental    = get_mental_health_recent()
        ctl, atl, tsb = calculate_tsb(load_data)
        print('Fetching training data...')
        training  = get_recent_training()
        print('Got training, fetching health...')
        health    = get_health_metrics()
        print('Got health, fetching load...')
        load_data = get_training_load()
        print('Got load, fetching mental...')
        mental    = get_mental_health_recent()
        print('Calling Claude...')
        ctl, atl, tsb = calculate_tsb(load_data)
        ...

        prompt = f"""You are a performance coach for a 47-year-old drilling engineer on sabbatical.
Goals: Body fat <15%, muscle 105-110 lbs, MS 150 bike ride April 25-26, Houston Marathon January 17 2027.
HR Zones: Z1<130, Z2 131-150, Z3 151-160, Z4 161-170, Z5>171
CTL: {ctl} | ATL: {atl} | TSB: {tsb}
Today: {TODAY()}

Recent training: {json.dumps(training, default=str)}
Health metrics: {json.dumps(health, default=str)}
Mental health: {json.dumps(mental, default=str)}

Question: {question}

Answer concisely in 3-5 sentences. Use actual data values. Be direct."""

        answer = ask_claude(prompt, max_tokens=300)
        send_message(answer)
    except Exception as e:
        send_message(f'⚠️ Error: {str(e)[:100]}')

# ── Incoming message handler ──────────────────────────────────
def handle_update(update):
    try:
        message = update.get('message', {})
        text    = message.get('text', '').strip()
    if not text:
        return

    print(f'Received: {text}')

    # Commands
    if text == '/start' or text == '/help':
        send_message(
            '*MS Performance Coach* 🏃‍♂️\n\n'
            'Commands:\n'
            '/morning — Morning check-in\n'
            '/afternoon — Afternoon check-in\n'
            '/evening — Evening check-in\n'
            '/briefing — Get tonight\'s briefing now\n'
            '/status — Quick training status\n\n'
            'Or just ask me anything about your training!'
        )
        return

    if text == '/morning':
        start_checkin('morning')
        return

    if text == '/afternoon':
        start_checkin('afternoon')
        return

    if text == '/evening':
        start_checkin('evening')
        return

    if text == '/briefing':
        send_message('Generating your briefing...')
        send_evening_briefing()
        return

    if text == '/status':
        try:
            load_data = get_training_load()
            health    = get_health_metrics()
            ctl, atl, tsb = calculate_tsb(load_data)
            latest_health = health[0] if health else {}
            tsb_label = (
                'Very fresh' if tsb > 10 else
                'Fresh' if tsb > 5 else
                'Neutral' if tsb > -10 else
                'Productive load' if tsb > -25 else
                'High fatigue'
            )
            send_message(
                f'*Quick Status — {TODAY()}*\n\n'
                f'CTL (fitness): {ctl}\n'
                f'ATL (fatigue): {atl}\n'
                f'TSB (form): {tsb} — {tsb_label}\n'
                f'HRV: {latest_health.get("hrv_ms", "—")} ms\n'
                f'Resting HR: {latest_health.get("resting_hr_bpm", "—")} bpm'
            )
        except Exception as e:
            send_message(f'⚠️ Error: {str(e)[:100]}')
        return

    # Mid check-in response
    if state['mode']:
        handle_checkin_response(text)
        return

    # Free-form question
    send_message('Let me check your data...')
    handle_question(text)

except Exception as e:
        print(f'handle_update error: {e}')
        import traceback
        traceback.print_exc()
        try:
            send_message(f'⚠️ Error: {str(e)[:100]}')
        except:
            pass
# ── Webhook endpoint ──────────────────────────────────────────
@app.route('/telegram', methods=['POST'])
def telegram_webhook():
    update = request.get_json(silent=True)
    if update:
        threading.Thread(target=handle_update, args=(update,)).start()
    return jsonify({'ok': True}), 200

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({'status': 'ok', 'service': 'telegram-bot'}), 200

@app.route('/trigger/briefing', methods=['POST'])
def manual_briefing():
    threading.Thread(target=send_evening_briefing).start()
    return jsonify({'status': 'triggered'}), 200

# ── Scheduler ─────────────────────────────────────────────────
def run_scheduler():
    # Times in UTC (CST = UTC-5, CDT = UTC-4)
    schedule.every().day.at('12:00').do(trigger_morning_checkin)    # 7am CDT
    schedule.every().day.at('18:00').do(trigger_afternoon_checkin)  # 1pm CDT
    schedule.every().day.at('23:30').do(trigger_evening_checkin)    # 6:30pm CDT
    schedule.every().day.at('00:00').do(trigger_evening_briefing)   # 7pm CDT

    while True:
        schedule.run_pending()
        time.sleep(30)

# ── Setup Telegram webhook ────────────────────────────────────
def setup_webhook(base_url):
    webhook_url = f'{base_url}/telegram'
    r = requests.post(f'{TELEGRAM_API}/setWebhook', json={'url': webhook_url})
    print(f'Webhook set: {r.json()}')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8081))

    # Start scheduler in background
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

    # Setup webhook if URL provided
    base_url = os.environ.get('RAILWAY_PUBLIC_URL', '')
    if base_url:
        setup_webhook(base_url)

    app.run(host='0.0.0.0', port=port)
