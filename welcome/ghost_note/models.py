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
TEST_TOKEN_DURATION = timezone.timedelta(hours=1, minutes=30)


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

    def commission_from_user(self, referred_user):
        total = self.commissions_earned.filter(referred_user=referred_user).aggregate(
            total=models.Sum('commission_amount')
        )['total']
        return total or Decimal('0.00')


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
        default=generate_access_token,
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
        if self.token_type == self.TokenType.TEST:
            self.payment_amount = None
            self.apply_test_token_schedule()
        self.sync_label_from_user()
        super().save(*args, **kwargs)
        self.sync_referral_commission()


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
