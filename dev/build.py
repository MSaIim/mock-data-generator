import shutil
import subprocess
import sys

# Build mockgen.zip for upload to S3: libraries plus the app code, all at the zip root

# Start clean
shutil.rmtree("build", ignore_errors=True)

# Install requirements.txt as Linux wheels that match the Lambda runtime (python3.13, x86_64), whatever OS this runs on.
# pyarrow and fastavro are compiled, so a plain local install would fetch the wrong binaries
platform = [
    "--platform", "manylinux2014_x86_64", "--platform", "manylinux_2_17_x86_64", "--platform", "manylinux_2_28_x86_64",
    "--implementation", "cp", "--python-version", "3.13", "--only-binary=:all:",
]
subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt", *platform, "--target", "build", "--quiet"], check=True)

# App code and UI (the local-only backend in src/local isn't needed in Lambda)
shutil.copytree("src", "build", dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "local"))

# Zip with forward-slash paths (PowerShell's Compress-Archive can write backslashes that break Lambda)
shutil.make_archive("lambda-mock-gen", "zip", "build")
print("Built lambda-mock-gen.zip")
