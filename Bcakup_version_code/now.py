import os
import json
import datetime
import logging
import time
from logging.handlers import RotatingFileHandler
from zk import ZK
import local_config
import requests

SYNC_INTERVAL = 5 * 60  
LAST_SYNC_FILE = 'last_sync_time.json'

device_punch_values_IN = getattr(local_config, 'device_punch_values_IN', [0, 4])
device_punch_values_OUT = getattr(local_config, 'device_punch_values_OUT', [1, 5])

def setup_logger(name, log_directory, level=logging.INFO):
    current_date = datetime.datetime.now().strftime('%d-%m-%Y')
    log_file = os.path.join(log_directory, f"{current_date}_{name}.log")

    formatter = logging.Formatter('%(asctime)s - %(message)s')
    handler = RotatingFileHandler(log_file, maxBytes=10_000_000, backupCount=50)
    handler.setFormatter(formatter)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    if not logger.hasHandlers():
        logger.addHandler(handler)

    return logger

if not os.path.exists(local_config.LOGS_DIRECTORY):
    os.makedirs(local_config.LOGS_DIRECTORY)

info_logger = setup_logger('biometric_info_logger', local_config.LOGS_DIRECTORY)
error_logger = setup_logger('_biometric_error_logger', local_config.LOGS_DIRECTORY, level=logging.ERROR)
attendance_success_logger = setup_logger('attendance_success_logger', local_config.LOGS_DIRECTORY)
attendance_failed_logger = setup_logger('attendance_failed_logger', local_config.LOGS_DIRECTORY)

def get_last_sync_time():
    if os.path.exists(LAST_SYNC_FILE):
        with open(LAST_SYNC_FILE, 'r') as f:
            return json.load(f).get('last_sync_time', None)
    return None

def update_last_sync_time():
    last_sync_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(LAST_SYNC_FILE, 'w') as f:
        json.dump({'last_sync_time': last_sync_time}, f)

def check_duplicate_entry(user_id, timestamp):
    sent_logs_file = os.path.join(local_config.LOGS_DIRECTORY, "sent_logs.json")
    if os.path.exists(sent_logs_file):
        try:
            with open(sent_logs_file, 'r') as f:
                sent_logs = json.load(f)
                return any(log['user_id'] == user_id and log['timestamp'] == timestamp for log in sent_logs)
        except Exception as e:
            error_logger.error(f"Error reading sent logs file: {e}")
    return False

def get_all_attendance_from_device(ip, device_id, last_sync_time):
    zk = ZK(ip)
    conn = None
    attendances = []
    try:
        conn = zk.connect()
        logs = conn.get_attendance()
        for log in logs:
            if log.timestamp > last_sync_time and not check_duplicate_entry(log.user_id, log.timestamp.strftime('%Y-%m-%d %H:%M:%S')):
                attendances.append(log)
    except Exception as e:
        error_logger.error(f"Error fetching data from device {ip}: {e}")
    finally:
        if conn:
            conn.disconnect()
    return attendances

def send_to_erpnext(employee, timestamp, log_type):
    try:
        url = local_config.ERPNEXT_URL + "/api/resource/Employee Checkin"
        headers = {
            'Authorization': f"token {local_config.ERPNEXT_API_KEY}:{local_config.ERPNEXT_API_SECRET}",
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        }
        data = {"employee": employee, "time": timestamp, "log_type": log_type}
        response = requests.post(url, headers=headers, json=data)
        if response.status_code == 200:
            return 200, response.json().get('data', {}).get('name', 'Success')
        else:
            return response.status_code, response.text
    except requests.exceptions.RequestException as e:
        return 500, str(e)

def export_biometric_data_and_exit(last_sync_time):
    date = datetime.datetime.now().strftime('%Y-%m-%d')
    output_file = os.path.join(local_config.LOGS_DIRECTORY, f"biometric_data_{date}.json")
    data_to_export = []
    failed_logs = []
    success_logs = []

    for device in local_config.devices:
        logs = get_all_attendance_from_device(device['ip'], device['device_id'], last_sync_time)
        for log in logs:
            punch_direction = 'IN' if 8 <= log.timestamp.hour < 15 else 'OUT'
            data_to_export.append({
                'user_id': f"T{int(log.user_id):06d}",
                'timestamp': log.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
                'log_type': punch_direction
            })

    sent_logs_file = os.path.join(local_config.LOGS_DIRECTORY, "sent_logs.json")
    sent_logs = []
    if os.path.exists(sent_logs_file):
        try:
            with open(sent_logs_file, 'r') as f:
                sent_logs = json.load(f)
        except Exception as e:
            error_logger.error(f"Error reading sent logs file: {e}")

    for log in data_to_export:
        if log in sent_logs:
            continue

        status_code, message = send_to_erpnext(log['user_id'], log['timestamp'], log['log_type'])
        if status_code == 200:
            success_logs.append(log)
            attendance_success_logger.info(f"Success: {log['user_id']} at {log['timestamp']} ({log['log_type']}) - {message}")
            sent_logs.append(log)
        else:
            failed_logs.append(log)
            attendance_failed_logger.error(f"Failed: {log['user_id']} at {log['timestamp']} ({log['log_type']}) - {message}")

        try:
            with open(sent_logs_file, 'w') as f:
                json.dump(sent_logs, f, indent=4)
        except Exception as e:
            error_logger.error(f"Error writing sent logs to file: {e}")

    update_last_sync_time()
    print(f"Biometric attendance pushed successfully to ERPNext with: {len(success_logs)}")

if __name__ == "__main__":
    while True:
        try:
            last_sync_time_str = get_last_sync_time()
            last_sync_time = datetime.datetime.strptime(last_sync_time_str, '%Y-%m-%d %H:%M:%S') if last_sync_time_str else datetime.datetime.now() - datetime.timedelta(days=1)
            export_biometric_data_and_exit(last_sync_time)
        except Exception as e:
            error_logger.error(f"Error during execution: {e}")
            print("Retrying in 5 minutes...")
            time.sleep(SYNC_INTERVAL)
