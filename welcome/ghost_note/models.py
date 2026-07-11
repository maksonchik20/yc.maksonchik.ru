import secrets
import string
import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from .referrals import calculate_commission

ACCESS_TOKEN_LENGTH = 6
ACCESS_TOKEN_ALPHABET = string.ascii_uppercase + string.digits
TEST_TOKEN_DURATION = timezone.timedelta(minutes=20)
TEST_TOKEN_COOLDOWN = timezone.timedelta(days=7)


def new_session_id():
    return str(uuid.uuid4())


def generate_access_token():
    for _ in range(64):
        token = ''.join(
            secrets.choice(ACCESS_TOKEN_ALPHABET)
            for _ in range(ACCESS_TOKEN_LENGTH)
        )
        if not GhostAccessToken.objects.filter(token=token).exists():
            return token
    raise RuntimeError('Unable to generate a unique access token')


REFERRAL_KEY_LENGTH = 8
REFERRAL_KEY_ALPHABET = string.ascii_uppercase + string.digits


def generate_referral_key():
    for _ in range(64):
        key = ''.join(
            secrets.choice(REFERRAL_KEY_ALPHABET)
            for _ in range(REFERRAL_KEY_LENGTH)
        )
        if not GhostUser.objects.filter(referral_key=key).exists():
            return key
    raise RuntimeError('Unable to generate a unique referral key')


class GhostUser(models.Model):
    name = models.CharField(max_length=128, verbose_name='Имя')
    telegram_username = models.CharField(
        max_length=64,
        blank=True,
        verbose_name='Telegram username',
        help_text='Без @, например: ivanov',
    )
    referred_by = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='referrals',
        verbose_name='Пригласил',
    )
    notes = models.TextField(blank=True, verbose_name='Заметки')
    referral_key = models.CharField(
        max_length=16,
        unique=True,
        blank=True,
        default='',
        editable=False,
        verbose_name='Реферальный ключ',
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Создан')

    class Meta:
        verbose_name = 'Пользователь Ghost Note'
        verbose_name_plural = 'Пользователи Ghost Note'
        ordering = ['name']

    def __str__(self):
        return self.name

    @property
    def telegram_url(self):
        username = self.telegram_username.lstrip('@').strip()
        if not username:
            return ''
        return f'https://t.me/{username}'

    def total_commission(self):
        total = self.commissions_earned.aggregate(total=models.Sum('commission_amount'))['total']
        return total or Decimal('0.00')

    def unpaid_commission_total(self):
        total = self.commissions_earned.filter(is_paid=False).aggregate(
            total=models.Sum('commission_amount')
        )['total']
        return total or Decimal('0.00')

    def paid_commission_total(self):
        total = self.commissions_earned.filter(is_paid=True).aggregate(
            total=models.Sum('commission_amount')
        )['total']
        return total or Decimal('0.00')

    def commission_from_user(self, referred_user):
        total = self.commissions_earned.filter(referred_user=referred_user).aggregate(
            total=models.Sum('commission_amount')
        )['total']
        return total or Decimal('0.00')

    def sync_referral_commissions(self):
        for token in self.tokens.filter(token_type='real'):
            token.sync_referral_commission()

    def save(self, *args, **kwargs):
        referred_by_changed = False
        if self.pk:
            old_referred_by_id = (
                GhostUser.objects.filter(pk=self.pk)
                .values_list('referred_by_id', flat=True)
                .first()
            )
            referred_by_changed = old_referred_by_id != self.referred_by_id
        else:
            referred_by_changed = bool(self.referred_by_id)

        if not self.referral_key:
            self.referral_key = generate_referral_key()

        super().save(*args, **kwargs)

        if referred_by_changed:
            self.sync_referral_commissions()


class GhostAccessToken(models.Model):
    class TokenType(models.TextChoices):
        TEST = 'test', 'Тестовый'
        REAL = 'real', 'Реальный'

    user = models.ForeignKey(
        GhostUser,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='tokens',
        verbose_name='Пользователь',
    )
    token_type = models.CharField(
        max_length=8,
        choices=TokenType.choices,
        default=TokenType.REAL,
        verbose_name='Тип токена',
    )
    payment_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name='Сумма оплаты',
        help_text='Только для реального токена',
    )
    token = models.CharField(
        max_length=64,
        unique=True,
        blank=True,
        default='',
        editable=False,
        verbose_name='Токен',
    )
    label = models.CharField(max_length=128, blank=True, verbose_name='Заметка')
    starts_at = models.DateTimeField(default=timezone.now, verbose_name='Действителен с')
    expires_at = models.DateTimeField(verbose_name='Действителен до')
    is_active = models.BooleanField(default=True, verbose_name='Активен')
    allow_local = models.BooleanField(default=True, verbose_name='Локальный доступ')
    allow_remote = models.BooleanField(default=True, verbose_name='Удалённый доступ')
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True, verbose_name='Последнее использование')

    class Meta:
        verbose_name = 'Токен доступа Ghost Note'
        verbose_name_plural = 'Токены доступа Ghost Note'
        ordering = ['-created_at']

    def __str__(self):
        label = self.label or (self.user.name if self.user_id else self.token)
        expires = timezone.localtime(self.expires_at)
        return f'{label} (до {expires:%d.%m.%Y %H:%M} МСК)'

    @property
    def is_valid(self):
        now = timezone.now()
        return self.is_active and self.starts_at <= now < self.expires_at

    def clean(self):
        super().clean()
        if self.token_type == self.TokenType.TEST:
            return
        if self.payment_amount is None:
            raise ValidationError({'payment_amount': 'Укажите сумму оплаты для реального токена.'})
        if self.payment_amount <= 0:
            raise ValidationError({'payment_amount': 'Сумма оплаты должна быть больше нуля.'})

    def apply_test_token_schedule(self):
        now = timezone.now()
        self.starts_at = now
        self.expires_at = now + TEST_TOKEN_DURATION

    def sync_label_from_user(self):
        if self.user_id and not self.label:
            self.label = self.user.name

    def sync_referral_commission(self):
        if self.token_type != self.TokenType.REAL:
            GhostReferralCommission.objects.filter(token=self).delete()
            return
        if not self.user_id or not self.user.referred_by_id or not self.payment_amount:
            GhostReferralCommission.objects.filter(token=self).delete()
            return

        referrer = self.user.referred_by
        commission_amount = calculate_commission(self.payment_amount)
        GhostReferralCommission.objects.update_or_create(
            token=self,
            defaults={
                'referrer': referrer,
                'referred_user': self.user,
                'payment_amount': self.payment_amount,
                'commission_amount': commission_amount,
            },
        )

    def save(self, *args, **kwargs):
        if not self.token:
            self.token = generate_access_token()
        if self.token_type == self.TokenType.TEST:
            self.payment_amount = None
        self.sync_label_from_user()
        super().save(*args, **kwargs)
        self.sync_referral_commission()


class GhostPurchaseOrder(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Ожидает оплаты'
        PAID = 'paid', 'Оплачен'
        CANCELED = 'canceled', 'Отменён'
        FAILED = 'failed', 'Ошибка'

    class AccessType(models.TextChoices):
        LOCAL = 'local', 'Локальный доступ'
        REMOTE = 'remote', 'Удалённый доступ'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    customer_name = models.CharField(max_length=128, verbose_name='Имя покупателя')
    customer_telegram = models.CharField(
        max_length=64,
        blank=True,
        default='',
        verbose_name='Telegram покупателя',
    )
    customer_email = models.EmailField(
        blank=True,
        default='',
        verbose_name='E-mail покупателя',
    )
    referral_key_input = models.CharField(
        max_length=16,
        blank=True,
        verbose_name='Введённый реферальный ключ',
    )
    referrer = models.ForeignKey(
        GhostUser,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='purchase_referrals',
        verbose_name='Пригласивший',
    )
    user = models.ForeignKey(
        GhostUser,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='purchase_orders',
        verbose_name='Пользователь',
    )
    access_type = models.CharField(
        max_length=8,
        choices=AccessType.choices,
        verbose_name='Тип доступа',
    )
    duration_minutes = models.PositiveSmallIntegerField(verbose_name='Длительность (мин)')
    starts_at = models.DateTimeField(verbose_name='Начало доступа')
    amount = models.DecimalField(max_digits=10, decimal_places=2, verbose_name='Сумма')
    yookassa_payment_id = models.CharField(
        max_length=64,
        blank=True,
        default='',
        db_index=True,
        verbose_name='ID платежа ЮKassa',
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        verbose_name='Статус',
    )
    token = models.OneToOneField(
        GhostAccessToken,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='purchase_order',
        verbose_name='Выданный токен',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    telegram_notified_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='Токен отправлен в Telegram',
    )
    telegram_notify_error = models.CharField(
        max_length=500,
        blank=True,
        default='',
        verbose_name='Ошибка отправки в Telegram',
    )
    email_notified_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='Токен отправлен на e-mail',
    )
    email_notify_error = models.CharField(
        max_length=500,
        blank=True,
        default='',
        verbose_name='Ошибка отправки на e-mail',
    )
    referrer_notified_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='Пригласивший уведомлён в Telegram',
    )
    referrer_notify_error = models.CharField(
        max_length=500,
        blank=True,
        default='',
        verbose_name='Ошибка уведомления пригласившего',
    )

    class Meta:
        verbose_name = 'Заказ Ghost Note'
        verbose_name_plural = 'Заказы Ghost Note'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.customer_name} — {self.get_access_type_display()} ({self.get_status_display()})'


class GhostTelegramContact(models.Model):
    telegram_user_id = models.BigIntegerField(unique=True, verbose_name='Telegram chat_id')
    username = models.CharField(
        max_length=64,
        blank=True,
        default='',
        db_index=True,
        verbose_name='Username',
    )
    updated_at = models.DateTimeField(auto_now=True)
    last_trial_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='Последний пробный доступ',
    )

    class Meta:
        verbose_name = 'Telegram-контакт Ghost Note'
        verbose_name_plural = 'Telegram-контакты Ghost Note'

    def __str__(self):
        if self.username:
            return f'@{self.username} ({self.telegram_user_id})'
        return str(self.telegram_user_id)


class GhostTelegramBotMessage(models.Model):
    class Direction(models.TextChoices):
        IN = 'in', 'Входящее'
        OUT = 'out', 'Исходящее'

    class MessageKind(models.TextChoices):
        TEXT = 'text', 'Текст'
        CALLBACK = 'callback', 'Callback'
        DOCUMENT = 'document', 'Документ'
        OTHER = 'other', 'Другое'

    telegram_user_id = models.BigIntegerField(db_index=True, verbose_name='Telegram chat_id')
    username = models.CharField(
        max_length=64,
        blank=True,
        default='',
        db_index=True,
        verbose_name='Username',
    )
    first_name = models.CharField(max_length=128, blank=True, default='', verbose_name='Имя')
    direction = models.CharField(max_length=3, choices=Direction.choices, verbose_name='Направление')
    message_kind = models.CharField(
        max_length=16,
        choices=MessageKind.choices,
        default=MessageKind.TEXT,
        verbose_name='Тип',
    )
    text = models.TextField(blank=True, default='', verbose_name='Текст')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='Когда')

    class Meta:
        verbose_name = 'Сообщение Ghost Note бота'
        verbose_name_plural = 'Сообщения Ghost Note бота'
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['username', 'created_at']),
            models.Index(fields=['telegram_user_id', 'created_at']),
        ]

    def __str__(self):
        who = f'@{self.username}' if self.username else str(self.telegram_user_id)
        arrow = '→' if self.direction == self.Direction.OUT else '←'
        preview = (self.text or '')[:60]
        return f'{arrow} {who}: {preview}'


class GhostReferralPayout(GhostUser):
    class Meta:
        proxy = True
        verbose_name = 'Выплата рефералу'
        verbose_name_plural = 'Выплаты рефералам'


class GhostRealPayment(GhostAccessToken):
    class Meta:
        proxy = True
        verbose_name = 'Реальная оплата'
        verbose_name_plural = 'Реальные оплаты'


class GhostReferralCommission(models.Model):
    referrer = models.ForeignKey(
        GhostUser,
        on_delete=models.CASCADE,
        related_name='commissions_earned',
        verbose_name='Кто привёл',
    )
    referred_user = models.ForeignKey(
        GhostUser,
        on_delete=models.CASCADE,
        related_name='commissions_generated',
        verbose_name='Кого привели',
    )
    token = models.OneToOneField(
        GhostAccessToken,
        on_delete=models.CASCADE,
        related_name='referral_commission',
        verbose_name='Токен покупки',
    )
    payment_amount = models.DecimalField(max_digits=10, decimal_places=2, verbose_name='Сумма покупки')
    commission_amount = models.DecimalField(max_digits=10, decimal_places=2, verbose_name='Комиссия (20%)')
    is_paid = models.BooleanField(default=False, verbose_name='Выплачено')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Создано')

    class Meta:
        verbose_name = 'Реферальная комиссия'
        verbose_name_plural = 'Реферальные комиссии'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.referrer.name} ← {self.referred_user.name}: {self.commission_amount}'


class GhostSession(models.Model):
    session_id = models.CharField(max_length=36, unique=True, default=new_session_id, editable=False)
    access_token = models.ForeignKey(
        GhostAccessToken,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sessions',
    )
    screenshot = models.BinaryField(null=True, blank=True)
    screenshot_updated_at = models.DateTimeField(null=True, blank=True)
    audio_enabled = models.BooleanField(default=False, verbose_name='Трансляция звука')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Ghost Note session'
        verbose_name_plural = 'Ghost Note sessions'

    def save_screenshot(self, data):
        self.screenshot = data
        self.screenshot_updated_at = timezone.now()
        self.save(update_fields=['screenshot', 'screenshot_updated_at', 'updated_at'])


class GhostTextMessage(models.Model):
    session = models.ForeignKey(GhostSession, on_delete=models.CASCADE, related_name='text_messages')
    text = models.TextField()
    delivered = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        verbose_name = 'Ghost Note text'
        verbose_name_plural = 'Ghost Note texts'
