@echo off
title DAS Link
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0das-link.ps1" %*
