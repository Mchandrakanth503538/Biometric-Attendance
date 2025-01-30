
import os
import json
import datetime
import logging
from logging.handlers import RotatingFileHandler
from zk import ZK
import local_config
import requests
import time


device_punch_values_IN = getattr(local_config, 'device_punch_values_IN', [0,4])
device_punch_values_OUT = getattr(local_config, 'device_punch_values_OUT', [1,5])

LAST_SYNC_FILE = 'last_sync_time.json'

def get_last_sync_time():
    if os.path.exists(LAST_SYNC_FILE):
        with open(LAST_SYNC_FILE, 'r') as f:
            return json.load(f).get('last_sync_time', None)
    return None

def update_last_sync_time():
    last_sync_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(LAST_SYNC_FILE, 'w') as f:
        json.dump({'last_sync_time': last_sync_time}, f)

def setup_logger(name, log_directory, level=logging.INFO):
    """Set up a JSON logger with a date-based log file name."""
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


# Set up loggers
if not os.path.exists(local_config.LOGS_DIRECTORY):
    os.makedirs(local_config.LOGS_DIRECTORY)

info_logger = setup_logger('biometric_info_logger', local_config.LOGS_DIRECTORY)
error_logger = setup_logger('_biometric_error_logger', local_config.LOGS_DIRECTORY, level=logging.ERROR)
attendance_success_logger = setup_logger('attendance_success_logger', local_config.LOGS_DIRECTORY)
attendance_failed_logger = setup_logger('attendance_failed_logger', local_config.LOGS_DIRECTORY)


def get_all_attendance_from_device(ip, device_id, last_sync_time):
    """Fetches attendance logs from the device after last_sync_time."""
    zk = ZK(ip, timeout=30)  
    conn = None
    attendances = []

    try:
        conn = zk.connect()
        time.sleep(2)  
        all_logs = conn.get_attendance()
        attendances = [log for log in all_logs if log.timestamp > last_sync_time]
    except Exception as e:
        error_logger.error(f"Error fetching data from device {ip}: {e}")
    finally:
        if conn:
            conn.disconnect()

    return attendances



def send_to_erpnext(employee, timestamp, log_type):
    """
    Send attendance data to ERPNext.
    """
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


def check_employee_status(employee):
    """Check if the employee is active in ERPNext."""
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


def process_biometric_data():
    """Process biometric data and push records to ERPNext."""
    last_sync_time = get_last_sync_time()
    print(f"Last sync time: {last_sync_time}")

    success_logs = []
    failed_logs = []
    not_active_logs = []

    # Collect attendance logs
    try:
        for device in local_config.devices:
            logs = get_all_attendance_from_device(device['ip'], device['device_id'], last_sync_time)
            for log in logs:
                punch_time = log.timestamp
                punch_hour = punch_time.hour
                punch_direction = 'IN' if 8 <= punch_hour < 15 else 'OUT'

                user_id = f"T{int(log.user_id):06d}"
                timestamp = punch_time.strftime('%Y-%m-%d %H:%M:%S')
                log_type = punch_direction

                if check_employee_status(user_id):
                    status_code, message = send_to_erpnext(user_id, timestamp, log_type)
                    if status_code == 200:
                        success_logs.append(log)
                        attendance_success_logger.info(f"Success: {user_id} at {timestamp} ({log_type}) - {message}")
                    else:
                        failed_logs.append(log)
                        attendance_failed_logger.error(f"Failed: {user_id} at {timestamp} ({log_type}) - {message}")
                else:
                    not_active_logs.append(log)
                    attendance_failed_logger.error(f"Not active: {user_id} at {timestamp} ({log_type})")

    except Exception as e:
        error_logger.error(f"Error processing logs: {e}")

    update_last_sync_time()

    print("\nSummary:")
    print(f" - Success: {len(success_logs)}")
    print(f" - Skipped: {len(failed_logs)}")
    print(f" - Employee Not Active / Employee not exist: {len(not_active_logs)}")
    print("Check logs for details.")
    print(f"Biometric attendance pushed successfully to ERPNext with: {len(success_logs)}")


if __name__ == "__main__":
    while True:
        process_biometric_data()
        print("\nWaiting for next sync...")
        time.sleep(60) 
