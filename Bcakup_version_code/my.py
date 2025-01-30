import os
import json
import datetime
import logging
import time
from logging.handlers import RotatingFileHandler
from zk import ZK
import local_config
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

SYNC_INTERVAL = 1 * 60  
LAST_SYNC_FILE = 'last_sync_time.json'

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


def check_employee_status(employee):
    try:
        url = local_config.ERPNEXT_URL + "/api/resource/Employee"
        headers = {
            'Authorization': f"token {local_config.ERPNEXT_API_KEY}:{local_config.ERPNEXT_API_SECRET}",
            'Accept': 'application/json'
        }
        params = {"filters": json.dumps({"employee": employee}), "fields": '["status"]'}
        response = requests.get(url, headers=headers, params=params)
        if response.status_code == 200:
            data = response.json().get('data', [])
            return bool(data and data[0].get('status') == 'Active')
        else:
            error_logger.error(f"Failed to check employee status for {employee}: {response.status_code} - {response.text}")
            return False
    except requests.exceptions.RequestException as e:
        error_logger.error(f"Request exception while checking status for {employee}: {e}")
        return False
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
    attendance_data = []
    for device in local_config.devices:
        zk = ZK(device['ip'])
        conn = None
        try:
            conn = zk.connect()
            logs = conn.get_attendance()
            for log in logs:
                if log.timestamp > last_sync_time:
                    user_id = f"T{int(log.user_id):06d}"
                    timestamp = log.timestamp.strftime('%Y-%m-%d %H:%M:%S')
                    punch_direction = 'IN' if 8 <= log.timestamp.hour < 17 else 'OUT'
                    attendance_data.append({"user_id": user_id, "timestamp": timestamp, "punch_direction": punch_direction})
        except Exception as e:
            error_logger.error(f"Error fetching data from device {device['ip']}: {e}")
        finally:
            if conn:
                conn.disconnect()
    
    for log in attendance_data:
        user_id, timestamp, punch_direction = log["user_id"], log["timestamp"], log["punch_direction"]
        if check_employee_status(user_id):
            status_code, message = send_to_erpnext(user_id, timestamp, punch_direction)
            if status_code == 200:
                attendance_success_logger.info(f"Success: {user_id} at {timestamp} ({punch_direction}) - {message}")
            else:
                attendance_failed_logger.error(f"Failed: {user_id} at {timestamp} ({punch_direction}) - {message}")
        else:
            attendance_failed_logger.error(f"Not active: {user_id} at {timestamp} ({punch_direction})")
    
    update_last_sync_time()

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