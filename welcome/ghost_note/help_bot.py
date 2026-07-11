HELP_CALLBACK_YES = 'help_yes'
HELP_CALLBACK_NO = 'help_no'


def help_confirm_text():
    return (
        '🆘 <b>Позвать оператора</b>\n\n'
        'Сначала составьте ваш вопрос и отправьте его сообщением в этот чат. '
        'После этого нажмите «Да», чтобы позвать оператора.\n\n'
        'Позвать оператора?'
    )


def help_confirm_keyboard():
    return {
        'inline_keyboard': [[
            {'text': 'Да', 'callback_data': HELP_CALLBACK_YES},
            {'text': 'Вернуться обратно', 'callback_data': HELP_CALLBACK_NO},
        ]],
    }


def help_back_text():
    return (
        'Хорошо. Если понадобится помощь — снова отправьте /help.\n\n'
        'Список команд: /start'
    )


def help_operator_called_text():
    return (
        '✅ <b>Оператор вызван</b>\n\n'
        'Мы получили ваш запрос. Время ответа может быть <b>до 2 часов</b>.\n\n'
        'Пока ждёте, можете посмотреть команды: /start'
    )
