#!/usr/bin/env bash
set -euo pipefail

# Ubuntu 18.04: ставит Python 3.9 и venv для Django-проекта welcome.
# Запуск на сервере:
#   curl -sSL <url>/scripts/setup_python39.sh | bash
# или:
#   bash scripts/setup_python39.sh

PROJECT_DIR="${PROJECT_DIR:-/usr/share/django-projects/welcome}"
PYTHON_VERSION="${PYTHON_VERSION:-3.9}"
VENV_DIR="${PROJECT_DIR}/venv"

if [[ "$(id -u)" -ne 0 ]]; then
  SUDO="sudo"
else
  SUDO=""
fi

echo "==> Установка зависимостей системы"
$SUDO apt-get update
$SUDO apt-get install -y software-properties-common \
  build-essential pkg-config libmysqlclient-dev \
  curl

if ! command -v "python${PYTHON_VERSION}" >/dev/null 2>&1; then
  echo "==> Установка Python ${PYTHON_VERSION} (deadsnakes PPA)"
  $SUDO add-apt-repository -y ppa:deadsnakes/ppa
  $SUDO apt-get update
  $SUDO apt-get install -y \
    "python${PYTHON_VERSION}" \
    "python${PYTHON_VERSION}-venv" \
    "python${PYTHON_VERSION}-dev" \
    "python${PYTHON_VERSION}-distutils"
fi

echo "==> Python: $(python${PYTHON_VERSION} --version)"

if [[ ! -d "${PROJECT_DIR}" ]]; then
  echo "Каталог проекта не найден: ${PROJECT_DIR}" >&2
  exit 1
fi

echo "==> Создание venv: ${VENV_DIR}"
python${PYTHON_VERSION} -m venv "${VENV_DIR}"
# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"

python -m pip install --upgrade pip
python -m pip install django dataclasses mysqlclient requests psutil

if [[ -f "${PROJECT_DIR}/../requirements.txt" ]]; then
  python -m pip install -r "${PROJECT_DIR}/../requirements.txt"
elif [[ -f "${PROJECT_DIR}/requirements.txt" ]]; then
  python -m pip install -r "${PROJECT_DIR}/requirements.txt"
fi

echo "==> Проверка Django"
cd "${PROJECT_DIR}"
python manage.py check

cat <<EOF

Готово.

Python: $(python --version)
Venv:   ${VENV_DIR}

Дальше:
  1. Обновите gunicorn/systemd, чтобы использовался:
       ${VENV_DIR}/bin/python
       ${VENV_DIR}/bin/gunicorn
  2. Перезапустите сервис приложения.
  3. При необходимости:
       source ${VENV_DIR}/bin/activate
       python manage.py migrate

Пример команды gunicorn:
  ${VENV_DIR}/bin/gunicorn welcome.wsgi:application --bind 127.0.0.1:8000
EOF
