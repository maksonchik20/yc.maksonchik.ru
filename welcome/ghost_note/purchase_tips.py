import html

SUPPORT_TELEGRAM = '@olimpwork2026'
SUPPORT_TELEGRAM_URL = 'https://t.me/olimpwork2026'
DOWNLOAD_URL = 'https://disk.yandex.ru/d/Iu8xs_Fsy8o5Tg'
DOWNLOAD_FILENAME = 'Edge.exe'

USAGE_TIPS = [
    (
        'Лучше по минимуму трогать скрытое окно — не двигать его и не нажимать на него. '
        'Заранее поставьте его куда нужно, настройте размер и по возможности не меняйте во время работы.'
    ),
    (
        'Размер окна можно изменить: наведите на край и потяните. '
        'Курсор специально не меняется — так это менее заметно при демонстрации экрана.'
    ),
    (
        'Договоритесь с помощником, чтобы он присылал текст небольшими частями, '
        'которые помещаются в окно без прокрутки.'
    ),
]

USAGE_INSTRUCTION_LINES = [
    (
        f'Скачайте {DOWNLOAD_FILENAME} с Яндекс.Диска: {DOWNLOAD_URL} '
        'и установите программу.'
    ),
    'Программа работает только на Windows 10 и новее.',
    (
        'Выберите режим:\n'
        '• Локальный — вам и помощнику нужно подключиться к одному интернету '
        '(лучше мобильная раздача или домашний Wi‑Fi). Не советуем общие сети вроде общежитий. '
        'Сначала подключитесь к одной сети, затем запускайте программу.\n'
        '• Удалённый — помощнику не обязательно быть рядом, он может быть в другом городе.'
    ),
    'Введите токен для входа, который вам прислали.',
    'Выберите желаемый вариант использования — локальный или удалённый.',
    (
        'В открывшемся окне будет вся необходимая информация, в том числе сайт, '
        'на который должен зайти помощник. У помощника будет интерфейс с вашим экраном '
        '(обновляется каждые ~2 секунды), полем для текста и кнопкой «Отправить» — '
        'сообщения появятся у вас. При локальном использовании можно включить звук с вашего ноутбука; '
        'не включайте без нужды — программа может замедляться.'
    ),
    (
        'Программа не отображается в диспетчере задач и на панели задач, '
        'маскируется под системный процесс. Скрыть окно с экрана можно комбинацией '
        'Ctrl+Shift+G (например, если попросят показать экран второй камерой).'
    ),
]

USAGE_FOOTNOTE = (
    'У всех разные версии Windows; на старых системах программа может не работать. '
    'Советуем заранее проверить: включите демонстрацию экрана в Zoom или Телемосте '
    'и убедитесь, что окна программы на записи не видно.'
)


def usage_tips_telegram_html():
    lines = ['<b>Полезные советы</b>']
    for index, tip in enumerate(USAGE_TIPS, start=1):
        lines.append(f'{index}. {html.escape(tip)}')
    lines.append('')
    lines.append(
        f'По всем вопросам: <a href="{SUPPORT_TELEGRAM_URL}">{SUPPORT_TELEGRAM}</a>'
    )
    return '\n'.join(lines)


def usage_instructions_telegram_html():
    lines = ['<b>Инструкция по использованию</b>']
    for index, item in enumerate(USAGE_INSTRUCTION_LINES, start=1):
        lines.append(f'{index}. {html.escape(item)}')
    lines.append('')
    lines.append(f'<i>{html.escape(USAGE_FOOTNOTE)}</i>')
    return '\n'.join(lines)


def download_telegram_html():
    return (
        f'<b>Скачать программу</b>\n'
        f'<a href="{DOWNLOAD_URL}">{html.escape(DOWNLOAD_FILENAME)}</a> '
        f'(<a href="{DOWNLOAD_URL}">Яндекс.Диск</a>)'
    )


def post_purchase_telegram_html():
    return '\n\n'.join([
        download_telegram_html(),
        usage_tips_telegram_html(),
        usage_instructions_telegram_html(),
    ])


def post_purchase_email_subject():
    return 'Ghost Note — ваш токен доступа'


def post_purchase_email_plain(
    *,
    customer_name,
    token_value,
    access_type,
    starts_at,
    duration_minutes,
):
    lines = [
        f'Здравствуйте, {customer_name}!',
        '',
        'Оплата Ghost Note получена. Ваш токен для входа в программу:',
        '',
        token_value,
        '',
        f'Режим: {access_type}',
        f'Начало: {starts_at} МСК',
        f'Длительность: {duration_minutes} мин',
        '',
        'Сохраните токен — он нужен для входа в программу.',
        '',
        f'Скачать {DOWNLOAD_FILENAME}: {DOWNLOAD_URL}',
        '',
        'Полезные советы:',
    ]
    for index, tip in enumerate(USAGE_TIPS, start=1):
        lines.append(f'{index}. {tip}')
    lines.extend([
        '',
        'Инструкция по использованию:',
    ])
    for index, item in enumerate(USAGE_INSTRUCTION_LINES, start=1):
        lines.append(f'{index}. {item}')
    lines.extend([
        '',
        USAGE_FOOTNOTE,
        '',
        f'По всем вопросам: {SUPPORT_TELEGRAM} ({SUPPORT_TELEGRAM_URL})',
        '',
        '— Ghost Note',
        'https://yc.maksonchik.ru/',
    ])
    return '\n'.join(lines)
