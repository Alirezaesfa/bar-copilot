# rules for this project
- this is a demo for an ibm interview. its a billing adjustment assistant for a made up telecom called maplewave. all the data is fake.
- i'm on windows 11 with powershell. the python venv is in the project folder (venv). write scripts in python, not bash.
- agents, tools and the knowledge base live in watsonx orchestrate and are built with the adk. follow the wxo-builder skill and check cli flags with --help before using them.
- the data is in watsonx.data (presto, iceberg). catalog and schema come from PRESTO_CATALOG and PRESTO_SCHEMA. only use table and column names from docs/DATA_DICTIONARY.md, plus review_decisions.
- never print or hard code keys. secrets come from .env on my laptop, or from the orchestrate connection maplewave_data when tools run in orchestrate.
- sql is read only and always uses parameters. the only writes are submit_request and record_decision.
- money math is done in python with Decimal, rounded half up to cents at the end. the model never does math, it calls a tool.
- keep the code simple and short. every function gets a one line docstring.
- after each change run the right check or test and show me the output. then tell me what you changed in 3 to 5 plain sentences.