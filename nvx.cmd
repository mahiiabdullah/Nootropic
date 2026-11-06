@echo off
rem Run the Neurovex CLI from the project's virtual environment, no activation needed.
rem Usage (from this folder):  .\nvx llm setup
"%~dp0.venv\Scripts\nvx.exe" %*
