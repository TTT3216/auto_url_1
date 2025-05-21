import requests
import schedule
import time
import threading
import uuid # UUID生成のために追加
from flask import Flask, render_template, request, jsonify
from datetime import datetime, time as dt_time # 時間比較のために追加

app = Flask(__name__)

# --- アプリケーションの状態を管理するグローバル変数 ---
# 複数のジョブを管理するための辞書
# キー: ジョブID (UUID), 値: { url, interval, unit, is_running, status_message, scheduled_job_obj, run_start_time, run_end_time, id_for_log, logged_paused_status }
app_jobs = {}
app_jobs_lock = threading.Lock() # 状態変更時の競合を防ぐためのロック

# スケジューラーループは一つで良い
scheduler_thread = None
scheduler_running = False # スケジューラーループ自体の実行状態

# --- コアロジック ---
def access_url_web(set_id):
    """指定されたジョブIDのURLにアクセスし、そのジョブのstatus_messageを更新します。"""
    job_details = None
    with app_jobs_lock:
        # ジョブがまだ存在し、かつ実行中であるかを確認
        if set_id in app_jobs and app_jobs[set_id].get("is_running", False): # is_runningの存在も確認
            job_details = app_jobs[set_id].copy() # 読み取り用にコピー
            url_to_access = job_details.get("url") # getで安全にアクセス
        else:
            # ジョブが見つからないか、既に停止されている場合は何もしない
            # print(f"Job {set_id} is not running or not found. Skipping access.")
            return

    if not url_to_access:
        with app_jobs_lock:
             if set_id in app_jobs:
                app_jobs[set_id]["status_message"] = "エラー: URLが設定されていません。"
        print(f"Job {job_details.get('id_for_log', set_id[:8])}: URL not set.")
        return

    currently_logging_pause = False

    run_start_time_str = job_details.get("run_start_time")
    run_end_time_str = job_details.get("run_end_time")

    if run_start_time_str and run_start_time_str.strip() and \
       run_end_time_str and run_end_time_str.strip():
        try:
            start_t = dt_time.fromisoformat(run_start_time_str)
            end_t = dt_time.fromisoformat(run_end_time_str)
            now_t = datetime.now().time()
            is_within_window = False
            if start_t <= end_t:
                if start_t <= now_t <= end_t:
                    is_within_window = True
            else:
                if now_t >= start_t or now_t <= end_t:
                    is_within_window = True
            
            if is_within_window:
                with app_jobs_lock:
                    if set_id in app_jobs:
                        app_jobs[set_id]['logged_paused_status'] = False
            
            if not is_within_window:
                with app_jobs_lock:
                    if set_id in app_jobs:
                        if not app_jobs[set_id].get('logged_paused_status', False):
                            current_time_str = time.strftime('%Y-%m-%d %H:%M:%S')
                            app_jobs[set_id]["status_message"] = f"一時停止中 (実行時間外: {run_start_time_str}-{run_end_time_str}) [{current_time_str}]"
                            print(f"Job {job_details.get('id_for_log', set_id[:8])}: {app_jobs[set_id]['status_message']}")
                            app_jobs[set_id]['logged_paused_status'] = True
                            currently_logging_pause = True
                if currently_logging_pause or (set_id in app_jobs and app_jobs[set_id].get('logged_paused_status', False)): # 再度app_jobsの存在確認
                    return
        except ValueError:
            print(f"Job {job_details.get('id_for_log', set_id[:8])}: 実行時間帯の形式が無効です ({run_start_time_str}, {run_end_time_str})。時間帯チェックを無視します。")

    with app_jobs_lock:
        if set_id in app_jobs:
            app_jobs[set_id]['logged_paused_status'] = False

    try:
        response = requests.get(url_to_access, timeout=10)
        with app_jobs_lock:
            if set_id in app_jobs:
                current_time_str = time.strftime('%Y-%m-%d %H:%M:%S')
                app_jobs[set_id]["status_message"] = f"アクセス成功: {url_to_access} (ステータス: {response.status_code}) [{current_time_str}]"
                print(f"Job {job_details.get('id_for_log', set_id[:8])}: {app_jobs[set_id]['status_message']}")
    except requests.exceptions.Timeout:
        with app_jobs_lock:
            if set_id in app_jobs:
                current_time_str = time.strftime('%Y-%m-%d %H:%M:%S')
                app_jobs[set_id]["status_message"] = f"タイムアウト: {url_to_access} へのアクセスに失敗しました。 [{current_time_str}]"
                print(f"Job {job_details.get('id_for_log', set_id[:8])}: {app_jobs[set_id]['status_message']}")
    except requests.exceptions.RequestException as e:
        with app_jobs_lock:
            if set_id in app_jobs:
                current_time_str = time.strftime('%Y-%m-%d %H:%M:%S')
                app_jobs[set_id]["status_message"] = f"エラー: {url_to_access} へのアクセスに失敗しました: {e} [{current_time_str}]"
                print(f"Job {job_details.get('id_for_log', set_id[:8])}: {app_jobs[set_id]['status_message']}")


def run_scheduler_loop():
    global scheduler_running
    print("Scheduler loop started.")
    scheduler_running = True
    while scheduler_running:
        schedule.run_pending()
        time.sleep(1)
    print("Scheduler loop ended.")


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/start', methods=['POST'])
def start_process():
    global app_jobs, scheduler_thread, scheduler_running
    data = request.json
    url = data.get('url')
    interval_str = data.get('interval')
    unit = data.get('unit')
    run_start_time = data.get('run_start_time', "")
    run_end_time = data.get('run_end_time', "")
    set_id = data.get('set_id')
    is_new_job = False

    if not url or not url.startswith(("http://", "https://")):
        return jsonify({"success": False, "message": "有効なURLを入力してください。"}), 400
    try:
        interval = int(interval_str)
        if interval <= 0:
            raise ValueError
    except (ValueError, TypeError):
        return jsonify({"success": False, "message": "時間間隔は正の整数で入力してください。"}), 400
    if unit not in ["seconds", "minutes"]:
        return jsonify({"success": False, "message": "無効な時間単位です。"}), 400

    with app_jobs_lock:
        if not set_id: 
            set_id = str(uuid.uuid4())
            is_new_job = True
            app_jobs[set_id] = { 
                "url": "", "interval": 0, "unit": "", "is_running": False, 
                "status_message": "準備完了", "scheduled_job_obj": None,
                "run_start_time": "", "run_end_time": "",
                "id_for_log": set_id[:8], "logged_paused_status": False
            }
            print(f"Created new job with ID: {set_id}")
        elif set_id not in app_jobs:
            is_new_job = True 
            app_jobs[set_id] = { 
                "url": "", "interval": 0, "unit": "", "is_running": False, 
                "status_message": "準備完了", "scheduled_job_obj": None,
                "run_start_time": "", "run_end_time": "",
                "id_for_log": set_id[:8], "logged_paused_status": False
            }
            print(f"Re-creating job with ID from frontend: {set_id}")
        
        current_job = app_jobs[set_id]

        if not is_new_job and current_job.get("is_running", False):
            if "一時停止中" in current_job.get("status_message", ""):
                # まずフロントエンドから送られてきた新しい設定値でジョブ情報を更新
                current_job["url"] = url
                current_job["interval"] = interval
                current_job["unit"] = unit
                current_job["run_start_time"] = run_start_time 
                current_job["run_end_time"] = run_end_time
                current_job["logged_paused_status"] = False # 一時停止ログフラグをリセット

                # 更新された実行時間帯で現在時刻が実行時間帯内か再度チェック
                run_start_time_str_updated = current_job.get("run_start_time") # 更新後の値を取得
                run_end_time_str_updated = current_job.get("run_end_time")     # 更新後の値を取得
                is_now_within_window = False
                if run_start_time_str_updated and run_start_time_str_updated.strip() and \
                   run_end_time_str_updated and run_end_time_str_updated.strip():
                    try:
                        start_t = dt_time.fromisoformat(run_start_time_str_updated)
                        end_t = dt_time.fromisoformat(run_end_time_str_updated)
                        now_t = datetime.now().time()
                        if start_t <= end_t:
                            if start_t <= now_t <= end_t:
                                is_now_within_window = True
                        else:
                            if now_t >= start_t or now_t <= end_t:
                                is_now_within_window = True
                    except ValueError:
                        pass
                else:
                    is_now_within_window = True # 時間帯未設定なら常に時間内

                if is_now_within_window:
                    # 時間内なので、既存のスケジュールをキャンセルして新しい設定で再スケジュール
                    if current_job.get("scheduled_job_obj"):
                        try:
                            schedule.cancel_job(current_job["scheduled_job_obj"])
                            print(f"Re-scheduling: Cancelled existing job object for ID: {current_job['id_for_log']}")
                        except Exception as e:
                            print(f"Re-scheduling: Error cancelling existing job object for ID {current_job['id_for_log']}: {e}")
                    # is_running は True のままなので、後続のスケジュール設定処理に進む
                    # この時点で即時実行も行う
                    threading.Thread(target=access_url_web, args=(set_id,), daemon=True).start()
                    # メッセージは後続で設定されるが、フロントがポーリング再開するようにsuccess:trueとset_idを返す
                    # return jsonify({"success": True, "message": f"設定ID {current_job['id_for_log']} の設定を更新し、処理を再開しました（時間内）。", "set_id": set_id})
                    # このreturnを削除し、後続の共通処理に任せる
                else:
                    return jsonify({"success": False, "message": f"設定ID {current_job['id_for_log']} は現在も実行時間外です。設定された時間（{run_start_time_str_updated}-{run_end_time_str_updated}）になれば自動的に処理が再開されます。"}), 400
            else: # 「一時停止中」ではないが、既に is_running == True の場合
                return jsonify({"success": False, "message": f"設定ID {current_job['id_for_log']} は既に実行中です。設定内容を変更するには一度停止してください。"}), 400

        # ここから下は、新規ジョブの場合、または一時停止中で時間内だった場合に共通で実行される
        current_job["url"] = url
        current_job["interval"] = interval
        current_job["unit"] = unit
        current_job["run_start_time"] = run_start_time
        current_job["run_end_time"] = run_end_time
        current_job["logged_paused_status"] = False
        current_job["is_running"] = True # 確実にTrueに設定

        # 既存のスケジュールがあればキャンセル (is_new_job でない場合、または一時停止からの再開の場合)
        if not is_new_job and current_job.get("scheduled_job_obj"): # is_new_jobの条件を削除、常にチェック
             if current_job.get("scheduled_job_obj"):
                try:
                    schedule.cancel_job(current_job["scheduled_job_obj"])
                    print(f"Cancelled existing job object for ID (before new schedule): {current_job['id_for_log']}")
                except Exception as e:
                    print(f"Error cancelling existing job object for ID (before new schedule) {current_job['id_for_log']}: {e}")
        
        task_to_run = lambda sid=set_id: access_url_web(sid)
        job_scheduler = schedule.every(current_job["interval"])
        if current_job["unit"] == "seconds":
            scheduled_job_obj = job_scheduler.seconds.do(task_to_run).tag(set_id)
        elif current_job["unit"] == "minutes":
            scheduled_job_obj = job_scheduler.minutes.do(task_to_run).tag(set_id)
        
        current_job["scheduled_job_obj"] = scheduled_job_obj
        status_msg = f"{current_job['interval']} {current_job['unit']}ごとにURLアクセスを開始 (ID: {current_job['id_for_log']})"
        if run_start_time and run_end_time: # リクエストされたrun_start_timeを使用
            status_msg += f" [実行時間: {run_start_time}-{run_end_time}]"
        current_job["status_message"] = status_msg

        if scheduler_thread is None or not scheduler_thread.is_alive():
            scheduler_thread = threading.Thread(target=run_scheduler_loop, daemon=True)
            scheduler_thread.start()
            print("Scheduler loop thread started.")

        # 新規ジョブの場合、または一時停止中で時間内だった場合は、ここで初回（または再開後初回）アクセス
        if is_new_job or ("一時停止中" in app_jobs[set_id].get("status_message_before_update","") and is_now_within_window): # is_now_within_windowはスコープ外なので注意
             # この条件分岐は複雑なので、一旦シンプルに初回実行は常にここで行う
             # ただし、一時停止からの再開で既に上で即時実行している場合は重複する可能性。
             # 上の即時実行をコメントアウトし、ここで一元化するか、フラグで制御する。
             # 今回は、上の即時実行はそのままに、ここでの初回実行は新規ジョブの場合のみに限定する方が安全かもしれない。
             # または、上の即時実行後にメッセージを設定して返し、ここでは何もしない。
             # 前回の修正で、一時停止からの再開の場合、上で即時実行し、returnしていた。
             # そのロジックを維持しつつ、設定更新と再スケジュールを共通化する。
             # 修正案：一時停止中で時間内だった場合、上で即時実行し、success:trueで返す。
             # それ以外（新規または停止からの開始）の場合は、この共通処理でスケジュールと初回実行。
             # しかし、設定変更を伴う再開の場合、スケジュールも更新したい。
             # そのため、一時停止中で時間内だった場合も、この共通処理を通るようにする。
             # 上の分岐で is_running = True にしているので、ここに来る。
             # 既に上で threading.Thread(target=access_url_web...) を呼んでいるので、ここでは不要。
             # ただし、is_new_job の場合のみ初回実行する。
            if is_new_job:
                 threading.Thread(target=task_to_run, daemon=True).start()


        log_message = f"Process started/updated for job {current_job['id_for_log']}: URL={url}, Interval={current_job['interval']} {current_job['unit']}"
        if current_job.get('run_start_time') and current_job.get('run_end_time'):
            log_message += f", Active Hours: {current_job['run_start_time']}-{current_job['run_end_time']}"
        print(log_message)

        return jsonify({"success": True, "message": current_job["status_message"], "set_id": set_id})


@app.route('/stop', methods=['POST'])
def stop_process():
    global app_jobs
    data = request.json
    set_id = data.get('set_id')

    if not set_id:
        return jsonify({"success": False, "message": "設定IDが必要です。"}), 400

    with app_jobs_lock:
        if set_id not in app_jobs:
            return jsonify({"success": False, "message": f"設定ID {set_id[:8] if set_id else 'N/A'} が見つかりません。"}), 404

        job_to_stop = app_jobs[set_id]

        if not job_to_stop.get("is_running", False): # is_runningの存在確認
             return jsonify({"success": False, "message": f"設定ID {job_to_stop.get('id_for_log', set_id[:8])} は既に停止中です。"}), 400

        if job_to_stop.get("scheduled_job_obj"):
            try:
                schedule.cancel_job(job_to_stop["scheduled_job_obj"])
                print(f"Cancelled job object for ID: {job_to_stop['id_for_log']}")
            except Exception as e:
                 print(f"Error cancelling job object for ID {job_to_stop['id_for_log']}: {e}")

        job_to_stop["scheduled_job_obj"] = None
        job_to_stop["is_running"] = False
        job_to_stop["status_message"] = f"URLアクセスを停止しました (ID: {job_to_stop['id_for_log']})"
        job_to_stop["logged_paused_status"] = False

        print(f"Process stopped for job {job_to_stop['id_for_log']}.")
        return jsonify({"success": True, "message": job_to_stop["status_message"]})

@app.route('/status', methods=['GET'])
def get_status():
    global app_jobs
    set_id_query = request.args.get('set_id')

    with app_jobs_lock:
        if set_id_query:
            if set_id_query in app_jobs:
                job = app_jobs[set_id_query]
                return jsonify({
                    "set_id": set_id_query,
                    "is_running": job.get("is_running", False),
                    "url": job.get("url", ""),
                    "interval": job.get("interval", 0),
                    "unit": job.get("unit", ""),
                    "status_message": job.get("status_message", "情報なし"),
                    "run_start_time": job.get("run_start_time", ""),
                    "run_end_time": job.get("run_end_time", "")
                })
            else:
                return jsonify({"success": False, "message": f"設定ID {set_id_query[:8] if set_id_query else 'N/A'} が見つかりません。"}), 404
        else:
             all_statuses = []
             for sid, job_data in app_jobs.items():
                 all_statuses.append({
                     "set_id": sid,
                     "is_running": job_data.get("is_running", False),
                     "url": job_data.get("url", ""),
                     "interval": job_data.get("interval", 0),
                     "unit": job_data.get("unit", ""),
                     "status_message": job_data.get("status_message", "情報なし"),
                     "run_start_time": job_data.get("run_start_time", ""),
                     "run_end_time": job_data.get("run_end_time", "")
                 })
             return jsonify(all_statuses)


import atexit
def shutdown_scheduler():
    global scheduler_running, scheduler_thread, app_jobs
    print("Shutting down scheduler...")
    scheduler_running = False
    schedule.clear()
    with app_jobs_lock:
        app_jobs.clear()
    if scheduler_thread and scheduler_thread.is_alive():
        scheduler_thread.join(timeout=5)
    print("Scheduler shut down.")

atexit.register(shutdown_scheduler)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)

# GunicornなどのWSGIサーバーを使用する場合、この app.run() は直接実行されない。
    # しかし、ローカル開発のために残しておくのは良い。
    # 本番環境ではGunicornが app オブジェクトを直接扱う。
    # RenderはPORT環境変数を設定するので、それに追従するなら以下のようにするが、Gunicorn経由なら不要。
    # port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False) # 本番を意識するならdebug=False
