# AI Contract Audit Regression Tests

A regression testing framework for evaluating LLM-based contract risk detection systems.

This tool sends contract files to an audit API and validates whether the AI correctly detects legal risks while avoiding hallucinations, duplication, and inconsistent outputs.

## Features

* Risk keyword validation
* Severity classification checks
* Duplicate issue detection
* Hallucination detection for clauses and quotes
* Structural consistency validation
* Rewrite sanity checks
* Determinism testing (runs tests twice to detect instability)

## How It Works

1. Contract files are loaded from the `test_corpus` directory.
2. Each contract is sent to an audit API.
3. The AI response is parsed into detected issues.
4. Multiple validation checks are applied.
5. Results are printed in a regression test table.

## Example Output

```
Test               Issues HIGH CRIT Risk Severity Dup Count Structural Halluc Determinism
nda_clean          1      0    0    PASS PASS     PASS PASS  PASS      PASS    PASS
nda_uncapped       2      1    0    PASS PASS     PASS PASS  PASS      PASS    PASS
```

## Configuration

The API endpoint is configurable using an environment variable.

Default endpoint:

```
http://localhost:8000/ask_file
```

Override with:

Linux / macOS

```
export AUDIT_API_URL=http://your-server/ask_file
```

Windows

```
set AUDIT_API_URL=http://your-server/ask_file
```

## Running the Tests

```
python run_regression_tests.py
```

## Test Corpus

Each contract requires:

* `.txt` file containing the contract
* `.expected.json` file describing expected detections

Example:

```
nda_example.txt
nda_example.expected.json

## Server Requirement

This regression test framework sends contract files to an audit API.
The API must be running before executing the tests.

By default the script expects the endpoint:

http://localhost:8000/ask_file

Example: running a local FastAPI / Uvicorn server

```bash
uvicorn main:app --host 127.0.0.1 --port 8000
```

Once the server is running, the regression suite can be executed with:

```bash
python run_regression_tests.py
```

If the API is hosted elsewhere, the endpoint can be configured using an environment variable:

Linux / macOS

```bash
export AUDIT_API_URL=http://your-server/ask_file
```

Windows

```bash
set AUDIT_API_URL=http://your-server/ask_file
```

```

## Purpose

This project demonstrates an evaluation framework for testing the reliability and consistency of LLM-based contract auditing systems.
