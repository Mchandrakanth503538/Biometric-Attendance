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


def send_email_notification(subject, body):
    """Send an email notification using configurations from local_config."""
    try:
        email_host = getattr(local_config, 'EMAIL_HOST', '')
        email_port = getattr(local_config, 'EMAIL_PORT', 587)
        email_username = getattr(local_config, 'EMAIL_USERNAME', '')
        email_password = getattr(local_config, 'EMAIL_PASSWORD', '')
        email_recipient = getattr(local_config, 'EMAIL_RECIPIENT', '')

        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = email_username
        msg['To'] = email_recipient

        with smtplib.SMTP(email_host, email_port) as server:
            server.starttls()
            server.login(email_username, email_password)
            server.sendmail(email_username, email_recipient, msg.as_string())

        info_logger.info(f"Email notification sent to {email_recipient}")
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
            time.sleep(delay)
    if conn:
        conn.disconnect()
    return attendances


def export_biometric_data_and_exit(last_sync_time):
    date = datetime.datetime.now().strftime('%Y-%m-%d')
    data_to_export = []

    for device in local_config.devices:
        logs = get_all_attendance_from_device(device['ip'], device['device_id'], last_sync_time)
        for log in logs:
            punch_time = log.timestamp
            data_to_export.append({
                'user_id': log.user_id,
                'timestamp': punch_time.strftime('%Y-%m-%d %H:%M:%S'),
                'punch_direction': log.punch_direction
            })

    if not data_to_export:
        error_logger.warning("No logs fetched.")
        return

    output_file = os.path.join(local_config.LOGS_DIRECTORY, f"biometric_data_{date}.json")
    with open(output_file, 'w') as f:
        json.dump(data_to_export, f, indent=4)

    subject = "Biometric Device Error Alert"
    body = f"The following errors occurred:\n\n{json.dumps(data_to_export, indent=4)}"
    send_email_notification(subject, body)


if __name__ == "__main__":
    while True:
        try:
            last_sync_time_str = get_last_sync_time()
            last_sync_time = datetime.datetime.strptime(
                last_sync_time_str, '%Y-%m-%d %H:%M:%S') if last_sync_time_str else datetime.datetime.now() - datetime.timedelta(days=1)
            export_biometric_data_and_exit(last_sync_time)
        except Exception as e:
            error_logger.error(f"Error during execution: {e}")
        time.sleep(SYNC_INTERVAL)
