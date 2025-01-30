import os
import logging
import subprocess
import time
from datetime import datetime

def setup_logger():
    log_directory = './logs'
    if not os.path.exists(log_directory):
        os.makedirs(log_directory)
    
    log_file = os.path.join(log_directory, 'device_status.log')
    logger = logging.getLogger('device_status_logger')
    logger.setLevel(logging.DEBUG)
    
    handler = logging.FileHandler(log_file)
    handler.setLevel(logging.DEBUG)
    
    formatter = logging.Formatter('%(asctime)s - %(message)s')
    handler.setFormatter(formatter)
    
    if not logger.hasHandlers():
        logger.addHandler(handler)
    
    return logger

def check_device_status(ip):
    try:
        # Use appropriate ping command for the OS
        command = ['ping', '-c', '1', '-W', '2', ip] if os.name != 'nt' else ['ping', '-n', '1', '-w', '2000', ip]
        response = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return response.returncode == 0
    except Exception as e:
        return False

def monitor_device(ip, logger):
    previous_status = None  
    logger.info("Device status monitoring started.")
    
    while True:
        current_status = check_device_status(ip)
        
        if current_status != previous_status:
            if current_status:
                logger.info(f"Device {ip} is reachable.")
            else:
                logger.error(f"Error fetching data from device {ip}: can't reach device (ping {ip}).")
            
            previous_status = current_status
        
        logger.debug(f"Checked device {ip} at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        time.sleep(180) 
if __name__ == "__main__":
    device_ip = '192.168.0.100'   
    logger = setup_logger()
    monitor_device(device_ip, logger)
