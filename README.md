# Google Form Filling System

A Playwright-based automation framework for Google Forms that can crawl form structure, generate reusable JSON configurations, support multi-section and branching workflows, and execute repeated automated submissions with diagnostics.

This project is built around two main scripts:

- `wizard.py` — crawls a Google Form, detects structure and constraints, and helps generate a reusable config
- `runner.py` — uses the config to execute automated submissions with retries, validation repair, and diagnostics

## Features

- Google Forms structure extraction
- Reusable JSON config generation
- Multi-step and branching form support
- Radio, checkbox, dropdown, short text, paragraph, date, time, and grid handling
- Validation repair and retry logic
- Randomized or persona-style input generation
- Diagnostics logging for debugging failed runs
- Learned constraints persistence across runs

## Tech Stack

- Python
- Playwright
- JSON configuration

## Project Structure

```bash
google-form-filling-system/
│
├── wizard.py
├── runner.py
├── requirements.txt
├── README.md
├── .gitignore
├── LICENSE
│
├── configs/
├── crawl_diagnostics/
├── runner_diagnostics/
└── samples/
