# Clone the repository if not already local
git clone https://github.com/Centrale73/ThermDash.git
cd ThermDash

# Create and activate virtual environment (Windows PowerShell)
python -m venv .venv
.venv\Scripts\Activate.ps1

# Install required packages
pip install fastapi uvicorn pydantic ecologits rich