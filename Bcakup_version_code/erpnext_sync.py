import os
import json
import datetime
import logging
from logging.handlers import RotatingFileHandler
from zk import ZK
import local_config as config

def setup_logger(name, log_directory, level=logging.INFO):
    """Set up a JSON logger with a date-based log file name."""
    current_date = datetime.datetime.now().strftime('%d-%m-%Y')
    log_file = os.path.join(log_directory, f"{current_date}_attendance_log.json")

    formatter = logging.Formatter('%(message)s')
    handler = RotatingFileHandler(log_file, maxBytes=10000000, backupCount=50)
    handler.setFormatter(formatter)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    if not logger.hasHandlers():
        logger.addHandler(handler)

    return logger

if not os.path.exists(config.LOGS_DIRECTORY):
    os.makedirs(config.LOGS_DIRECTORY)

info_logger = setup_logger('info_logger', config.LOGS_DIRECTORY)

def get_all_attendance_from_device(ip, device_id):
    """Fetches attendance logs from the device."""
    zk = ZK(ip)
    conn = None
    attendances = []
    try:
        conn = zk.connect()
        attendances = conn.get_attendance()
    except Exception as e:
        print(f"Error fetching data from device {ip}: {e}")
    finally:
        if conn:
            conn.disconnect()
    return attendances

def export_biometric_data(date):
    """Export biometric data for the given date to a JSON file."""
    output_file = os.path.join(config.LOGS_DIRECTORY, f"biometric_data_{date}.json")
    data_to_export = []

    try:
        for device in config.devices:
            print(f"Processing device {device['device_id']}...")
            logs = get_all_attendance_from_device(device['ip'], device['device_id'])

            filtered_logs = [
                {
                    'user_id': f"T{int(log.user_id):06d}",
                    'timestamp': log.timestamp.strftime('%Y-%m-%d %H:%M:%S')
                }
                for log in logs if log.timestamp.strftime('%Y-%m-%d') == date
            ]

            data_to_export.extend(filtered_logs)
    except Exception as e:
        print(f"Error exporting data: {e}")
        return

    try:
        with open(output_file, 'w') as f:
            json.dump(data_to_export, f, indent=4)
        print(f"Biometric data successfully exported to {output_file}")
    except Exception as e:
        print(f"Error writing to file: {e}")

def cli_menu():
    """Displays the CLI menu and handles user input."""
    print("Welcome to Biometric Integration to Kernel")
    input("Press Enter to continue...")

    while True:
        print("\nPlease select an option:")
        print("1. Get data from Biometric")
        print("2. Exit")

        choice = input("Enter your choice (1/2): ").strip()

        if choice == '1':
            selected_date = input("Enter the date (DD-MM-YYYY): ").strip()
            try:
                datetime.datetime.strptime(selected_date, '%Y-%m-%d')
                export_biometric_data(selected_date)
            except ValueError:
                print("Invalid date format. Please try again.")

        elif choice == '2':
            print("Exiting the program. Goodbye!")
            break

        else:
            print("Invalid choice. Please try again.")

if __name__ == "__main__":
    cli_menu()
