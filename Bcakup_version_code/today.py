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

SYNC_INTERVAL = 3 * 60  
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
    date = datetime.datetime.now().strftime('%Y-%m-%d')
    output_file = os.path.join(local_config.LOGS_DIRECTORY, f"biometric_data_{date}.json")
    
    failed_logs = []
    not_active_logs = []
    success_logs = []
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
                    
                    attendance_data.append({
                        "user_id": user_id,
                        "timestamp": timestamp,
                        "punch_direction": punch_direction
                    })
        except Exception as e:
            error_logger.error(f"Error fetching data from device {device['ip']}: {e}")
        finally:
            if conn:
                conn.disconnect()
    
    with open(output_file, 'w') as f:
        json.dump(attendance_data, f, indent=4)
    
    for log in attendance_data:
        user_id, timestamp, punch_direction = log["user_id"], log["timestamp"], log["punch_direction"]
        if check_employee_status(user_id):
            status_code, message = send_to_erpnext(user_id, timestamp, punch_direction)
            if status_code == 200:
                success_logs.append(user_id)
                attendance_success_logger.info(f"Success: {user_id} at {timestamp} ({punch_direction}) - {message}")
            else:
                failed_logs.append(user_id)
                attendance_failed_logger.error(f"Failed: {user_id} at {timestamp} ({punch_direction}) - {message}")
        else:
            not_active_logs.append(user_id)
            attendance_failed_logger.error(f"Not active: {user_id} at {timestamp} ({punch_direction})")
    
    update_last_sync_time()
    
    print("\nSummary:")
    print(f" - Last sync time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f" - Not active: {len(not_active_logs)}")
    print(f" - Failed to push: {len(failed_logs)}")
    print(f" - Successfully pushed: {len(success_logs)}")

def get_recent_errors():
    current_date = datetime.datetime.now().strftime('%d-%m-%Y') 
    error_log_file = os.path.join(local_config.LOGS_DIRECTORY, f"{current_date}__biometric_error_logger.log")

    recent_errors = []
    try:
        if os.path.exists(error_log_file):
            with open(error_log_file, 'r') as f:
                lines = f.readlines()
                recent_errors = lines[-5:] 
        else:
            recent_errors = ["No recent errors found."]
    except Exception as e:
        error_logger.error(f"Failed to read error log: {e}")
        recent_errors = [f"Error reading log: {e}"]

    return '\n'.join(recent_errors)

if __name__ == "__main__":
    while True:
        try:
            last_sync_time_str = get_last_sync_time()
            last_sync_time = datetime.datetime.strptime(last_sync_time_str, '%Y-%m-%d %H:%M:%S') if last_sync_time_str else datetime.datetime.now() - datetime.timedelta(days=1)
            export_biometric_data_and_exit(last_sync_time)
        except Exception as e:
            error_logger.error(f"Error during execution: {e}")
            recent_errors = get_recent_errors() 
            send_email("Biometric device Execution Error", f"An error occurred during execution: {e}\n\nRecent Errors:\n{recent_errors}")
            print("Retrying in 3 minutes...")
            time.sleep(SYNC_INTERVAL)


