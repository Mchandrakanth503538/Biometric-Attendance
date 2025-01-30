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

def get_all_attendance_from_device(ip, device_id, last_sync_time, retries=3, delay=5):
    """Fetch attendance logs from the device with retry logic."""
    zk = ZK(ip)
    conn = None
    attendances = []
    attempt = 0 
    while attempt < retries:
        try:
            conn = zk.connect()
            logs = conn.get_attendance()
            for log in logs:
                if isinstance(last_sync_time, str):
                    last_sync_time_datetime = datetime.datetime.strptime(last_sync_time, '%Y-%m-%d %H:%M:%S')
                else:
                    last_sync_time_datetime = last_sync_time
                if log.timestamp > last_sync_time_datetime:
                    attendances.append(log)
            break
        except Exception as e:
            error_logger.error(f"Error fetching data from device {ip}: {e}")
            attempt += 1
            if attempt < retries:
                error_logger.info(f"Retrying in {delay} seconds...")
                time.sleep(delay)
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
def export_biometric_data_and_exit(last_sync_time):
    """Export biometric data for the date and exit after summary."""
    date = datetime.datetime.now().strftime('%Y-%m-%d')
    print(f"Processing biometric data for date: {date}")
    print(f"Please wait a moment, the attendance is being processed into ERPNext...")

    output_file = os.path.join(local_config.LOGS_DIRECTORY, f"biometric_data_{date}.json")
    sent_records_file = os.path.join(local_config.LOGS_DIRECTORY, f"sent_records_{date}.json")
    data_to_export = []
    failed_logs = []
    not_active_logs = []
    success_logs = []

    # Load sent records
    sent_records = set()
    if os.path.exists(sent_records_file):
        try:
            with open(sent_records_file, 'r') as f:
                sent_records = set(json.load(f))
        except Exception as e:
            error_logger.error(f"Error reading sent records file {sent_records_file}: {e}")

    # Collect attendance logs
    try:
        for device in local_config.devices:
            logs = get_all_attendance_from_device(device['ip'], device['device_id'], last_sync_time)
            filtered_logs = []
            for log in logs:
                punch_time = log.timestamp
                punch_hour = punch_time.hour

                # Determine punch direction based on time
                punch_direction = 'IN' if 8 <= punch_hour < 15 else 'OUT'

                log_id = f"{log.user_id}_{log.timestamp.strftime('%Y-%m-%d %H:%M:%S')}"
                if log_id not in sent_records:
                    filtered_log = {
                        'user_id': f"T{int(log.user_id):06d}",
                        'timestamp': punch_time.strftime('%Y-%m-%d %H:%M:%S'),
                        'punch_direction': punch_direction,
                        'log_type': punch_direction
                    }
                    filtered_logs.append(filtered_log)

            data_to_export.extend(filtered_logs)
    except Exception as e:
        error_logger.error(f"Error collecting logs: {e}")
        return

    # Check if the output file exists and load existing data if it does
    if os.path.exists(output_file):
        try:
            with open(output_file, 'r') as f:
                existing_data = json.load(f)
            data_to_export = existing_data + data_to_export  # Append new data to existing data
        except Exception as e:
            error_logger.error(f"Error reading existing file {output_file}: {e}")
            return

    # Save attendance logs to a file (append the new records)
    try:
        with open(output_file, 'w') as f:
            json.dump(data_to_export, f, indent=4)
    except Exception as e:
        error_logger.error(f"Error writing logs to file: {e}")

    total_logs = len(data_to_export)
    if total_logs > 0:
        print("\n[********* Sending logs to ERPNext]")

    for i, log in enumerate(data_to_export):
        percentage = int((i + 1) / total_logs * 100)
        print(f"\r[********* Sending {percentage}%]", end="")

        user_id = log['user_id']
        timestamp = log['timestamp']
        log_type = log['log_type']
        log_id = f"{user_id}_{timestamp}"
        if check_employee_status(user_id):
            status_code, message = send_to_erpnext(user_id, timestamp, log_type)
            if status_code == 200:
                success_logs.append(log)
                sent_records.add(log_id)
                attendance_success_logger.info(f"Success: {user_id} at {timestamp} ({log_type}) - {message}")
            else:
                failed_logs.append(log)
                attendance_failed_logger.error(f"Failed: {user_id} at {timestamp} ({log_type}) - {message}")
        else:
            not_active_logs.append(log)
            attendance_failed_logger.error(f"Not active: {user_id} at {timestamp} ({log_type})")

    # Save sent records to a file
    try:
        with open(sent_records_file, 'w') as f:
            json.dump(list(sent_records), f)
    except Exception as e:
        error_logger.error(f"Error writing sent records to file: {e}")

    # Export Summary
    print("\nSummary:")
    print(f" - Success: {len(success_logs)}")
    print(f" - Skipped: {len(failed_logs)}")
    print(f" - Employee Not Active / Employee not exist: {len(not_active_logs)}")
    print("Check logs for details.")
    print(f"Biometric attendance pushed successfully to ERPNext with: {len(success_logs)}")
    update_last_sync_time()


if __name__ == "__main__":
    while True:
        try:
            last_sync_time_str = get_last_sync_time()
            last_sync_time = last_sync_time_str if last_sync_time_str else (datetime.datetime.now() - datetime.timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
            export_biometric_data_and_exit(last_sync_time)
        except Exception as e:
            error_logger.error(f"Error during execution: {e}")
            print("Retrying in 5 minutes...")
            time.sleep(SYNC_INTERVAL)
