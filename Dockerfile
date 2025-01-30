# Use an official Python runtime as a parent image
FROM python:3.12-slim
RUN apt update && apt install -y iputils-ping
RUN apt-get update \
&& apt-get install -y curl jq \
&& ln -sf /usr/share/zoneinfo/Asia/Kolkata /etc/localtime

# Set the working directory in the container
WORKDIR /app

# Copy the current directory contents into the container
COPY . /app

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Make the script executable
RUN chmod +x biometric_attendance_sync.py

# Command to run the script
CMD ["python", "biometric_attendance_sync.py"]

