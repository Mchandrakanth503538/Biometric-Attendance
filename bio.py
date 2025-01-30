import os
import json
import datetime
import logging
import time
import sys
from logging.handlers import RotatingFileHandler
from zk import ZK
import local_config
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

SYNC_INTERVAL = 2 * 60  # 3 minutes
LAST_SYNC_FILE = 'last_sync_time.json'

device_punch_values_IN = getattr(local_config, 'device_punch_values_IN', [0, 4])
device_punch_values_OUT = getattr(local_config, 'device_punch_values_OUT', [1, 5])

EMAIL_SENDER = local_config.EMAIL_SENDER
EMAIL_RECEIVER = local_config.EMAIL_RECEIVER
SMTP_SERVER = local_config.SMTP_SERVER
SMTP_PORT = local_config.SMTP_PORT
SMTP_USER = local_config.SMTP_USER
SMTP_PASSWORD = local_config.SMTP_PASSWORD

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

def send_email(subject, body):
    try:
        msg = MIMEMultipart()
        msg['From'] = EMAIL_SENDER
        msg['To'] = EMAIL_RECEIVER
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))
        
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls() 
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())
            server.quit()
    except Exception as e:
        error_logger.error(f"Failed to send email: {e}")

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
    zk = ZK(ip,timeout=60)
    conn = None
    attendances = []
    attempt = 0 
    while attempt < retries:
        try:
            conn = zk.connect()
            logs = conn.get_attendance()
            for log in logs:
                if log.timestamp > last_sync_time:
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
def record_exists_in_erpnext(employee, timestamp):
    """Check if an attendance record already exists in ERPNext."""
    try:
        url = local_config.ERPNEXT_URL + "/api/resource/Employee Checkin"
        headers = {
            'Authorization': f"token {local_config.ERPNEXT_API_KEY}:{local_config.ERPNEXT_API_SECRET}",
            'Accept': 'application/json'
        }
        params = {
            "filters": json.dumps({"employee": employee, "time": timestamp}),
            "fields": '["name"]'
        }
        response = requests.get(url, headers=headers, params=params)
        
        if response.status_code == 200:
            data = response.json().get('data', [])
            return bool(data)  # True if record exists, False otherwise
        else:
            error_logger.error(f"Failed to check existing record for {employee} at {timestamp}: {response.text}")
            return False
    except requests.exceptions.RequestException as e:
        error_logger.error(f"Request exception while checking record for {employee}: {e}")
        return False


def send_to_erpnext(employee, timestamp, log_type):
    """Send new attendance record to ERPNext only if it does not already exist."""
    if record_exists_in_erpnext(employee, timestamp):
        info_logger.info(f"Skipping record for {employee} at {timestamp} - already exists in ERPNext.")
        return 200, "Record already exists"

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
    """Export biometric data and exit on success."""
    date = datetime.datetime.now().strftime('%Y-%m-%d')
    print(f"Processing biometric data for date: {date}")
    print(f"Please wait a moment, the attendance is being processed into TSL kernel...")

    success_logs = []

    try:
        for device in local_config.devices:
            logs = get_all_attendance_from_device(device['ip'], device['device_id'], last_sync_time)

            for log in logs:
                user_id = f"T{int(log.user_id):06d}"
                timestamp = log.timestamp.strftime('%Y-%m-%d %H:%M:%S')

                log_type = "IN" if 8 <= log.timestamp.hour < 15 else "OUT"

                if check_employee_status(user_id):
                    status_code, message = send_to_erpnext(user_id, timestamp, log_type)
                    if status_code == 200:
                        success_logs.append(log)

    except Exception as e:
        error_logger.error(f"Error processing biometric data: {e}")
        raise  # Let the main loop handle retry

    print("\n[********* Sending logs to kernel]")
    print("\nSummary:")
    print(f" - Last sync time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f" - Successfully pushed: {len(success_logs)}")

    if success_logs:
        update_last_sync_time()
        sys.exit(0)  
    else:
        raise Exception("No records pushed, retrying...")  
def get_recent_errors():
    """Retrieve recent errors from log file."""
    current_date = datetime.datetime.now().strftime('%d-%m-%Y') 
    error_log_file = os.path.join(local_config.LOGS_DIRECTORY, f"{current_date}__biometric_error_logger.log")

    try:
        if os.path.exists(error_log_file):
            with open(error_log_file, 'r') as f:
                return '\n'.join(f.readlines()[-5:])
    except Exception as e:
        error_logger.error(f"Failed to read error log: {e}")

    return "No recent errors found."

if __name__ == "__main__":
    while True:
        try:
            last_sync_time_str = get_last_sync_time()
            last_sync_time = datetime.datetime.strptime(last_sync_time_str, '%Y-%m-%d %H:%M:%S') if last_sync_time_str else datetime.datetime.now() - datetime.timedelta(days=1)

            export_biometric_data_and_exit(last_sync_time) 
        except Exception as e:
            error_logger.error(f"Error during execution: {e}")
            recent_errors = get_recent_errors() 

            send_email(
                "Biometric Device Execution Error",
                f"An error occurred during execution:\n\n{e}\n\nRecent Errors:\n{recent_errors}"
            )

            print("❌ Error encountered! Retrying in 2 minutes...")
            time.sleep(SYNC_INTERVAL)  # Retry after 3 minutes
