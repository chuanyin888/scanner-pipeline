@echo off
chcp 65001 >nul
title 区域卡生成器
py "%~dp0make_card.py" --interactive
pause
