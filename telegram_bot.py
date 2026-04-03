import os
import json
import requests
import schedule
import time
import threading
import traceback
from datetime import date, datetime, timedelta
from flask import Flask, request, jsonify

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
SUPABASE_URL     = os.environ.get('SUPABASE_URL')
SUPABASE_KEY     = os.environ.get('SUPABASE_KEY')
ANTHROPIC_KEY    = os.environ.get('ANTHROPIC_API_KEY')
TELEGRAM_API     = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}'

SB_HEADERS = {
    'apikey':        SUPABASE_KEY,
    'Authorization': f'Bearer {SUPABASE_KEY}',
    'Content-Type':  'application/json',
    'Prefer':        'resolution=merge-duplicates'
}

TODAY = lambda: date.today().isoformat()

# ── Conversation state ────────────────────────────────────────
state = {'mode': None, 'step': None, 'data': {}}

# ── Telegram helpers ──────────────────────────────────────────
def send_message(text, reply_markup=None):
    try:
        payload = {
            'chat_id':    TELEGRAM_CHAT_ID,
            'text':       text,
            'parse_mode': 'Markdown'
        }
        if reply_markup:
            payload['reply_markup'] = json.dumps(reply_markup)
        r = requests.post(f'{TELEGRAM_API}/sendMessage', json=payload, timeout=10)
        print(f'send_message status: {r.status_code}')
        return r.json()
    except Exception as e:
        print(f'send_message error: {e}')

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
    print(f'Running query: {sql[:80]}...')
    r = requests.post(
        f'{SUPABASE_URL}/rest/v1/rpc/run_query',
        headers=SB_HEADERS,
        json={'query_text': sql},
        timeout=15
    )
    r.raise_for_status()
    result = r.json()
    print(f'Query returned {len(result) if isinstance(result, list) else 1} rows')
    return result

def insert(table, row):
    r = requests.post(
        f'{SUPABASE_URL}/rest/v1/{table}',
        headers={**SB_HEADERS, 'Prefer': 'return=minimal'},
        json=[row],
        timeout=10
    )
    r.raise_for_status()

# ── Claude helper ─────────────────────────────────────────────
def ask_claude(prompt, max_tokens=500):
    print('Calling Claude...')
    r = requests.post(
        'https://api.anthropic.com/v1/messages',
        headers={
            'Content-Type':      'application/json',
            'x-api-key':         ANTHROPIC_KEY,
            'anthropic-version': '2023-06-01'
        },
        json={
            'model':      'claude-sonnet-4-20250514',
            'max_tokens': max_tokens,
            'messages':   [{'role': 'user', 'content': prompt}]
        },
        timeout=45
    )
    r.raise_for_status()
    result = ''.join(b.get('text', '') for b in r.json().get('content', []))
    print(f'Claude responded with {len(result)} chars')
    return result

# ── Data fetchers ─────────────────────────────────────────────
def get_recent_training():
    return run_query("""
        SELECT date, run_min, ride_min, strength_min, cardio_min,
               z1_min, z2_min, z3_min, z4_min, z5_min,
               total_calories_kcal, steps
        FROM daily_activity_summary
        WHERE date >= CURRENT_DATE - INTERVAL '7 days'
        ORDER BY date DESC LIMIT 7
    """)

def get_health_metrics():
    return run_query("""
        SELECT date, resting_hr_bpm, hrv_ms, steps, active_calories_kcal
        FROM daily_health
        WHERE date >= CURRENT_DATE - INTERVAL '7 days'
        ORDER BY date DESC LIMIT 7
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
    try:
        return run_query("""
            SELECT date, time_of_day, energy, mood, stress, sleep_quality, notes
            FROM mental_health_logs
            WHERE date >= CURRENT_DATE - INTERVAL '3 days'
            ORDER BY logged_at DESC LIMIT 10
        """)
    except Exception as e:
        print(f'mental_health_logs error (table may not exist yet): {e}')
        return []

def get_body_comp():
    return run_query("""
        SELECT date, weight_lb, body_fat_pct, skeletal_muscle_mass_lb, inbody_score
        FROM body_composition ORDER BY date DESC LIMIT 1
    """)

def check_nutrition_logged_today():
    try:
        result = run_query(f"SELECT COUNT(*) as cnt FROM daily_nutrition WHERE date = '{TODAY()}'")
        return result[0]['cnt'] > 0 if result else False
    except Exception:
        return False

def get_rides_for_question(month=None, year=None):
    """Fetch ride data for distance questions."""
    if month and year:
        sql = f"""
            SELECT date, sport_type, distance_miles, moving_time_min, name
            FROM workouts_strava
            WHERE sport_type IN ('Ride','GravelRide','VirtualRide')
            AND EXTRACT(MONTH FROM date) = {month}
            AND EXTRACT(YEAR FROM date) = {year}
            ORDER BY date
        """
    else:
        sql = """
            SELECT date, sport_type, distance_miles, moving_time_min, name
            FROM workouts_strava
            WHERE sport_type IN ('Ride','GravelRide','VirtualRide')
            AND date >= CURRENT_DATE - INTERVAL '30 days'
            ORDER BY date
        """
    return run_query(sql)

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
                            ['run_min', 'ride_min', 'strength_min', 'cardio_min'])
            total = (total_min / 60) * (0.65 ** 2) * 100
        return total

    start = date.today() - timedelta(days=41)
    tss_by_date = {r['date']: calc_tss(r) for r in (activity_data or [])}

    atl = ctl = 0.0
    atl_decay = 1 - (1 / 7)
    ctl_decay = 1 - (1 / 42)

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
        training         = get_recent_training()
        health           = get_health_metrics()
        load_data        = get_training_load()
        nutrition        = get_nutrition_recent()
        mental           = get_mental_health_recent()
        body_comp        = get_body_comp()
        nutrition_logged = check_nutrition_logged_today()
        ctl, atl, tsb    = calculate_tsb(load_data)

        tsb_label = (
            'Very fresh — ready to perform' if tsb > 10 else
            'Fresh — good to train hard'    if tsb > 5  else
            'Neutral'                        if tsb > -10 else
            'Productive training load'       if tsb > -25 else
            'High fatigue — consider recovery'
        )

        prompt = f"""You are a performance coach for a 47-year-old drilling engineer on sabbatical.

ATHLETE PROFILE:
- Current phase: Body Comp + MS 150 (ride April 25-26)
- Goals: Body fat <15%, muscle 105-110 lbs
- History: Overtraining prone, needs structured progression
- HR Zones: Z1<130, Z2 131-150, Z3 151-160, Z4 161-170, Z5>171
- Today: {TODAY()}

FITNESS STATE: CTL {ctl} | ATL {atl} | TSB {tsb} ({tsb_label})

LAST 7 DAYS TRAINING: {json.dumps(training, default=str)}
HEALTH METRICS: {json.dumps(health, default=str)}
RECENT NUTRITION: {json.dumps(nutrition, default=str)}
Nutrition logged today: {nutrition_logged}
RECENT MENTAL HEALTH: {json.dumps(mental, default=str)}
BODY COMP: {json.dumps(body_comp, default=str)}

Generate a concise evening briefing. Format exactly like this:

*Evening Briefing — {date.today().strftime('%A, %B %d')}*

*Recovery Status:* [1-2 sentences on HRV, resting HR, TSB]

*Tomorrow's Recommendation:* [ONE of: Complete Rest / Active Recovery / Z2 Cardio / Z2 Ride / Strength Upper / Strength Lower / Tempo Run / Long Ride]

*Why:* [2-3 sentences based on data]

*Focus:* [1-2 specific actionable tips]

{f'*Nutrition reminder:* Log your food today!' if not nutrition_logged else 'Nutrition logged today.'}

Keep under 200 words. Be direct."""

        briefing = ask_claude(prompt, max_tokens=400)
        send_message(briefing)
        print('Evening briefing sent.')

    except Exception as e:
        print(f'Evening briefing error: {e}')
        traceback.print_exc()
        send_message(f'Could not generate briefing: {str(e)[:100]}')

# ── Check-in flows ────────────────────────────────────────────
CHECKIN_FLOWS = {
    'morning': [
        ('sleep_quality', '🌅 *Morning check-in!*\n\nHow did you sleep?\n_(1 = terrible, 10 = perfect)_', [1,2,3,4,5,6,7,8,9,10]),
        ('energy',        'Energy level right now?\n_(1 = exhausted, 10 = great)_', [1,2,3,4,5,6,7,8,9,10]),
        ('stress',        'Stress level this morning?\n_(1 = very relaxed, 10 = very stressed)_', [1,2,3,4,5,6,7,8,9,10]),
        ('notes',         'Any notes? _(injuries, poor sleep, etc.)_ — or reply *skip*', None),
    ],
    'afternoon': [
        ('energy', '☀️ *Afternoon check-in!*\n\nEnergy level right now?\n_(1 = exhausted, 10 = great)_', [1,2,3,4,5,6,7,8,9,10]),
        ('stress', 'Stress level?\n_(1 = very relaxed, 10 = very stressed)_', [1,2,3,4,5,6,7,8,9,10]),
        ('notes',  'Any notes? — or reply *skip*', None),
    ],
    'evening': [
        ('mood',   '🌙 *Evening check-in!*\n\nOverall mood today?\n_(1 = rough day, 10 = great day)_', [1,2,3,4,5,6,7,8,9,10]),
        ('energy', 'Energy level at end of day?\n_(1 = drained, 10 = still energized)_', [1,2,3,4,5,6,7,8,9,10]),
        ('stress', 'Stress level today overall?\n_(1 = very relaxed, 10 = very stressed)_', [1,2,3,4,5,6,7,8,9,10]),
        ('notes',  'Any notes about today? — or reply *skip*', None),
    ]
}

def start_checkin(time_of_day):
    state['mode'] = time_of_day
    state['step'] = 0
    state['data'] = {'time_of_day': time_of_day, 'date': TODAY()}
    ask_next_question()

def ask_next_question():
    flow = CHECKIN_FLOWS.get(state['mode'], [])
    if state['step'] >= len(flow):
        finish_checkin()
        return
    field, question, options = flow[state['step']]
    if options:
        send_quick_replies(question, options)
    else:
        remove_keyboard(question)

def handle_checkin_response(text):
    flow = CHECKIN_FLOWS.get(state['mode'], [])
    if state['step'] >= len(flow):
        return
    field, question, options = flow[state['step']]
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
        remove_keyboard('Check-in saved! Talk later.')
    except Exception as e:
        print(f'finish_checkin error: {e}')
        traceback.print_exc()
        send_message(f'Could not save check-in: {str(e)[:100]}')
    state['mode'] = None
    state['step'] = None
    state['data'] = {}

# ── Quick question handler ────────────────────────────────────
def handle_question(question):
    print(f'handle_question: {question}')
    try:
        print('Fetching training data...')
        training = get_recent_training()
        print('Fetching health metrics...')
        health = get_health_metrics()
        print('Fetching training load...')
        load_data = get_training_load()
        print('Fetching mental health...')
        mental = get_mental_health_recent()
        print('Calculating TSB...')
        ctl, atl, tsb = calculate_tsb(load_data)

        # For ride/mileage questions, fetch ride data too
        extra = ''
        q_lower = question.lower()
        if any(w in q_lower for w in ['mile', 'ride', 'bike', 'march', 'cycling', 'distance']):
            print('Fetching ride data...')
            # detect month reference
            month_map = {
                'january': 1, 'february': 2, 'march': 3, 'april': 4,
                'may': 5, 'june': 6, 'july': 7, 'august': 8,
                'september': 9, 'october': 10, 'november': 11, 'december': 12
            }
            month = None
            for name, num in month_map.items():
                if name in q_lower:
                    month = num
                    break
            year = 2026
            rides = get_rides_for_question(month=month, year=year)
            extra = f'\nRide data: {json.dumps(rides, default=str)}'

        prompt = f"""You are a performance coach for a 47-year-old drilling engineer on sabbatical.
Goals: Body fat <15%, muscle 105-110 lbs, MS 150 bike April 25-26, Houston Marathon Jan 17 2027.
HR Zones: Z1<130, Z2 131-150, Z3 151-160, Z4 161-170, Z5>171
CTL: {ctl} | ATL: {atl} | TSB: {tsb}
Today: {TODAY()}

Recent training: {json.dumps(training, default=str)}
Health metrics: {json.dumps(health, default=str)}
Mental health: {json.dumps(mental, default=str)}{extra}

Question: {question}

Answer concisely in 3-5 sentences. Use actual data values. Be direct."""

        print('Calling Claude for question...')
        answer = ask_claude(prompt, max_tokens=300)
        send_message(answer)
        print('Question answered.')
    except Exception as e:
        print(f'handle_question error: {e}')
        traceback.print_exc()
        send_message(f'Error answering question: {str(e)[:150]}')

# ── Incoming message handler ──────────────────────────────────
def handle_update(update):
    try:
        message = update.get('message', {})
        text    = message.get('text', '').strip()

        if not text:
            return

        print(f'Received: {text}')

        if text in ('/start', '/help'):
            send_message(
                '*MS Performance Coach*\n\n'
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
            threading.Thread(target=send_evening_briefing, daemon=True).start()
            return

        if text == '/status':
            try:
                load_data = get_training_load()
                health    = get_health_metrics()
                ctl, atl, tsb = calculate_tsb(load_data)
                latest_health = health[0] if health else {}
                tsb_label = (
                    'Very fresh'     if tsb > 10  else
                    'Fresh'          if tsb > 5   else
                    'Neutral'        if tsb > -10  else
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
                print(f'/status error: {e}')
                traceback.print_exc()
                send_message(f'Error: {str(e)[:100]}')
            return

        # Mid check-in response
        if state['mode']:
            handle_checkin_response(text)
            return

        # Free-form question — run in thread so we don't block
        send_message('Checking your data...')
        threading.Thread(target=handle_question, args=(text,), daemon=True).start()

    except Exception as e:
        print(f'handle_update error: {e}')
        traceback.print_exc()
        try:
            send_message(f'Something went wrong: {str(e)[:100]}')
        except Exception:
            pass

# ── Webhook endpoint ──────────────────────────────────────────
@app.route('/telegram', methods=['POST'])
def telegram_webhook():
    update = request.get_json(silent=True)
    if update:
        threading.Thread(target=handle_update, args=(update,), daemon=True).start()
    return jsonify({'ok': True}), 200

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({'status': 'ok', 'service': 'telegram-bot'}), 200

@app.route('/trigger/briefing', methods=['POST'])
def manual_briefing():
    threading.Thread(target=send_evening_briefing, daemon=True).start()
    return jsonify({'status': 'triggered'}), 200

# ── Scheduler ─────────────────────────────────────────────────
def run_scheduler():
    # UTC times (CDT = UTC-5)
    schedule.every().day.at('12:00').do(lambda: threading.Thread(target=start_checkin, args=('morning',), daemon=True).start())
    schedule.every().day.at('18:00').do(lambda: threading.Thread(target=start_checkin, args=('afternoon',), daemon=True).start())
    schedule.every().day.at('23:30').do(lambda: threading.Thread(target=start_checkin, args=('evening',), daemon=True).start())
    schedule.every().day.at('00:00').do(lambda: threading.Thread(target=send_evening_briefing, daemon=True).start())
    while True:
        schedule.run_pending()
        time.sleep(30)

# ── Setup Telegram webhook ────────────────────────────────────
def setup_webhook(base_url):
    webhook_url = f'{base_url}/telegram'
    r = requests.post(f'{TELEGRAM_API}/setWebhook', json={'url': webhook_url}, timeout=10)
    print(f'Webhook set: {r.json()}')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8081))
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    base_url = os.environ.get('RAILWAY_PUBLIC_URL', '')
    if base_url:
        setup_webhook(base_url)
    app.run(host='0.0.0.0', port=port)
