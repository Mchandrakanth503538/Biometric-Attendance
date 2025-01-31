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
def cleanup_old_biometric_files():
    """Deletes old biometric_data_{date}.json files at the end of the day."""
    current_date = datetime.datetime.now().strftime('%Y-%m-%d')
    for file in os.listdir(local_config.LOGS_DIRECTORY):
        if file.startswith("biometric_data_") and file.endswith(".json"):
            date_part = file.replace("biometric_data_", "").replace(".json", "")
            try:
                file_date = datetime.datetime.strptime(date_part, '%d-%m-%Y') if '-' in date_part and date_part[2] == '-' else datetime.datetime.strptime(date_part, '%Y-%m-%d')
                if file_date.strftime('%Y-%m-%d') < current_date:
                    file_path = os.path.join(local_config.LOGS_DIRECTORY, file)
                    os.remove(file_path) 
                    info_logger.info(f"Deleted old biometric data file: {file}")
            except ValueError:
                error_logger.error(f"Unexpected file format: {file}") 
                continue
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
            return bool(data)  
        else:
            error_logger.error(f"Failed to check existing record for {employee} at {timestamp}: {response.text}")
            return False
    except requests.exceptions.RequestException as e:
        error_logger.error(f"Request exception while checking record for {employee}: {e}")
        return False


def send_to_erpnext(employee, timestamp, log_type):
    """Send new attendance record to ERPNext only if it does not already exist."""
    if record_exists_in_erpnext(employee, timestamp):
        attendance_failed_logger.error(f"Skipped: {employee} at {timestamp} ({log_type}) - Record already exists")
        return 409, "Record already exists"   


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
    """Export biometric data for the date and exit after summary."""
    date = datetime.datetime.now().strftime('%Y-%m-%d')
    print(f"Processing biometric data for date: {date}")
    print(f"Please wait a moment ############...")

    output_file = os.path.join(local_config.LOGS_DIRECTORY, f"biometric_data_{date}.json")
    data_to_export = []
    failed_logs = []
    not_active_logs = []
    success_logs = []

    try:
        for device in local_config.devices:
            logs = get_all_attendance_from_device(device['ip'], device['device_id'], last_sync_time)
            filtered_logs = []

            for log in logs:
                punch_time = log.timestamp
                punch_hour = punch_time.hour
                punch_direction = 'IN' if 8 <= punch_hour < 17 else 'OUT'
                filtered_logs.append({
                    'user_id': f"T{int(log.user_id):06d}",
                    'timestamp': punch_time.strftime('%Y-%m-%d %H:%M:%S'),
                    'punch_direction': punch_direction,
                    'log_type': punch_direction
                })
            data_to_export.extend(filtered_logs)
    except Exception as e:
        error_logger.error(f"Error collecting logs: {e}")
        return
    if os.path.exists(output_file):
        try:
            with open(output_file, 'r') as f:
                existing_data = json.load(f)
            data_to_export = existing_data + data_to_export  
        except Exception as e:
            error_logger.error(f"Error reading existing file {output_file}: {e}")
            return
    unique_data = {f"{log['user_id']}_{log['timestamp']}": log for log in data_to_export}
    data_to_export = list(unique_data.values())
    try:
        with open(output_file, 'w') as f:
            json.dump(data_to_export, f, indent=4)
    except Exception as e:
        error_logger.error(f"Error writing logs to file: {e}")

    total_logs = len(data_to_export)
    if total_logs > 0:
        print("\n[********* Sending logs to kernel]")
    new_logs = [log for log in data_to_export if log in filtered_logs]

    for i, log in enumerate(new_logs):
        percentage = int((i + 1) / len(new_logs) * 100)
        print(f"\r[********* Sending {percentage}%]", end="")

        user_id = log['user_id']
        timestamp = log['timestamp']
        log_type = log['log_type']

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

    print("\nSummary:")
    print(f" - Last sync time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f" - Not active: {len(not_active_logs)}")
    print(f" - Failed to push: {len(failed_logs)}")
    print(f" - Successfully pushed: {len(success_logs)}")  
    update_last_sync_time()
    if success_logs:
        update_last_sync_time()
        exit(0)  
    else:
        attendance_success_logger.info(f"There is no records exist from Last sync time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}") 
    exit(0)
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
     cleanup_old_biometric_files()
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
            time.sleep(SYNC_INTERVAL)  

