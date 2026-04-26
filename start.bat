@echo off
echo Starting Drone Telemetry Dashboard...
start python server/drone_server.py
timeout /t 2
start client/index.html