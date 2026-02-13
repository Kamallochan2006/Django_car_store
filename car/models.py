from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal
import re

# Create your models here.

class Car(models.Model):
    FUEL_CHOICES = [
        ('petrol', 'Petrol'),
        ('diesel', 'Diesel'),
        ('electric', 'Electric'),
        ('hybrid', 'Hybrid'),
        ('cng', 'CNG'),
    ]
    
    TRANSMISSION_CHOICES = [
        ('manual', 'Manual'),
        ('automatic', 'Automatic'),
    ]
    
    name = models.CharField(max_length=200)
    brand = models.CharField(max_length=100)
    model_year = models.IntegerField()
    price = models.DecimalField(max_digits=12, decimal_places=2)
    selling_price = models.DecimalField(
        max_digits=12, 
        decimal_places=2, 
        null=True, 
        blank=True,
        help_text='Selling price (if different from marked price)'
    )
    emi_interest_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        help_text='Optional EMI interest rate (%) overriding the default'
    )
    fuel_type = models.CharField(max_length=20, choices=FUEL_CHOICES)
    transmission = models.CharField(max_length=20, choices=TRANSMISSION_CHOICES)
    mileage = models.CharField(max_length=50)
    engine = models.CharField(max_length=100)
    image = models.ImageField(upload_to='cars/', blank=True, null=True)
    description = models.TextField()
    is_available = models.BooleanField(default=True)
    stock = models.PositiveIntegerField(default=0, help_text='Number of cars in stock')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"{self.brand} {self.name} ({self.model_year})"
    
    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        """Save the car."""
        super().save(*args, **kwargs)

    @property
    def image_src(self) -> str:
        """Return the image URL or empty string for frontend handling."""
        # First check the main car image field
        if self.image:
            try:
                return self.image.url
            except ValueError:
                pass
                
        # Previously we fell back to first gallery image, but that causes issues 
        # where one color's variant shows another color's image on load.
        # Now we return empty string so frontend can show "No Image" placeholder.
        return ''

    @property
    def mileage_display(self) -> str:
        """Return mileage text, hiding per-litre suffix for EVs."""
        value = (self.mileage or '').strip()
        if not value:
            return ''
        if self.fuel_type == 'electric':
            pattern = re.compile(r'(?:km\s*/\s*litre|km\s*/\s*liter|km/l|kmpl|/litre|/liter)', re.IGNORECASE)
            value = pattern.sub('', value).strip(' -/\t')
        return value

    @property
    def fuel_icon_class(self) -> str:
        """Return icon class based on fuel type for template rendering."""
        mapping = {
            'electric': 'fas fa-bolt',
            'hybrid': 'fas fa-leaf',
            'cng': 'fas fa-leaf',
        }
        return mapping.get(self.fuel_type, 'fas fa-gas-pump')

    @property
    def price_value(self) -> Decimal:
        """Return normalized price in rupees, using selling_price if available, otherwise price."""
        # Use selling_price if available, otherwise use price
        main_price = self.selling_price if self.selling_price else self.price
        
        # Convert to Decimal safely
        try:
            raw_price = Decimal(main_price or 0)
        except:
            return Decimal('0.00')

        if raw_price <= 0:
            return Decimal('0.00')
            
        # --- FIX: Removed the automatic 'Crore' conversion logic ---
        # The database should hold the exact value (e.g., 20000000, not 2.0).
        # We rely on the CSV import script to do the scaling, not the model.
        
        return raw_price.quantize(Decimal('0.01'))

    @property
    def price_display(self) -> str:
        """Return price in Indian format (Lakh/Cr), using selling_price if available."""
        price_val = self.price_value
        if price_val >= 20000000:  # 2 Crores
            return f"₹{price_val / 10000000:.2f} Crores"
        elif price_val >= 10000000:  # 1 Crore
            return f"₹{price_val / 10000000:.2f} Crore"
        elif price_val >= 200000:  # 2 Lakhs
            return f"₹{price_val / 100000:.2f} Lakhs"
        elif price_val >= 100000:  # 1 Lakh
            return f"₹{price_val / 100000:.2f} Lakh"
        else:
            return f"₹{price_val:,.2f}"

    @property
    def average_rating(self) -> float:
        """Return the average rating from reviews, or 0 if no reviews."""
        reviews = self.reviews.all()
        if not reviews.exists():
            return 0
        total = sum(r.rating for r in reviews)
        return round(total / reviews.count(), 1)

    @property
    def review_count(self) -> int:
        """Return the number of reviews for this car."""
        return self.reviews.count()

    @property
    def total_stock(self) -> int:
        """Return total stock by summing all color variant stocks."""
        return sum(color.stock for color in self.colors.filter(is_available=True))
    
    @property
    def any_color_available(self) -> bool:
        """Return True if any color variant is available."""
        return self.colors.filter(is_available=True, stock__gt=0).exists()


class CarColor(models.Model):
    """Model to store available colors for a car."""
    car = models.ForeignKey(Car, on_delete=models.CASCADE, related_name='colors')
    name = models.CharField(max_length=50, help_text='Color name (e.g., Midnight Black)')
    hex_code = models.CharField(max_length=7, blank=True, default='', help_text='Optional hex color code (e.g., #000000)')
    stock = models.PositiveIntegerField(default=1, help_text='Stock quantity for this color')
    is_available = models.BooleanField(default=True, help_text='Is this color variant available?')
    order = models.PositiveIntegerField(default=0, help_text='Display order (lower = first)')
    
    class Meta:
        ordering = ['order', 'name']
        verbose_name = 'Car Color'
        verbose_name_plural = 'Car Colors'
        unique_together = ['car', 'name']
    
    def __str__(self):
        status = "Available" if self.is_available and self.stock > 0 else "Out of Stock"
        return f"{self.car.name} - {self.name} ({status})"
    
    def save(self, *args, **kwargs):
        """Save the car color."""
        super().save(*args, **kwargs)


class CarImage(models.Model):
    """Model to store multiple images for a car."""
    car = models.ForeignKey(Car, on_delete=models.CASCADE, related_name='images')
    car_color = models.ForeignKey(CarColor, on_delete=models.CASCADE, related_name='images', null=True, blank=True, help_text='Color variant for this image')
    image = models.ImageField(upload_to='car_images/', blank=True, null=True, help_text='Upload car image')
    caption = models.CharField(max_length=200, blank=True, help_text='Optional caption for this image')
    is_primary = models.BooleanField(default=False, help_text='Set as the main display image')
    order = models.PositiveIntegerField(default=0, help_text='Display order (lower = first)')
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['order', '-is_primary', 'created_at']
        verbose_name = 'Car Image'
        verbose_name_plural = 'Car Images'
    
    def __str__(self):
        color_name = self.car_color.name if self.car_color else 'No color'
        return f"{self.car.name} - {color_name} - Image {self.order}"
    
    @property
    def image_url(self):
        """Return image URL for templates (backwards compatibility)."""
        if self.image:
            try:
                return self.image.url
            except ValueError:
                pass
        return ''
    
    def save(self, *args, **kwargs):
        # If this is set as primary, unset others
        if self.is_primary:
            CarImage.objects.filter(car=self.car, is_primary=True).exclude(pk=self.pk).update(is_primary=False)
        super().save(*args, **kwargs)


class Customer(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, null=True, blank=True)
    name = models.CharField(max_length=200)
    email = models.EmailField()
    phone = models.CharField(max_length=15)
    address = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return self.name


class Payment(models.Model):
    PAYMENT_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]
    
    PAYMENT_METHOD_CHOICES = [
        ('cash', 'Cash'),
        ('card', 'Credit/Debit Card'),
        ('netbanking', 'Net Banking'),
        ('cheque', 'Cheque'),
        ('stripe', 'Stripe'),
    ]
    
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)
    car = models.ForeignKey(Car, on_delete=models.CASCADE)
    car_color = models.ForeignKey('CarColor', on_delete=models.SET_NULL, null=True, blank=True, related_name='payments')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES)
    payment_status = models.CharField(max_length=20, choices=PAYMENT_STATUS_CHOICES, default='pending')
    transaction_id = models.CharField(max_length=100, blank=True)
    stripe_session_id = models.CharField(max_length=200, blank=True, help_text='Stripe Checkout Session ID')
    payment_date = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"Payment - {self.customer.name} - ₹{self.amount}"


class EMIPlan(models.Model):
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('completed', 'Completed'),
        ('closed', 'Closed'),
        ('defaulted', 'Defaulted'),
    ]

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name='emi_plans')
    car = models.ForeignKey(Car, on_delete=models.CASCADE, related_name='emi_plans')
    car_color = models.ForeignKey('CarColor', on_delete=models.SET_NULL, null=True, blank=True, related_name='emi_plans')
    payment = models.OneToOneField('Payment', on_delete=models.SET_NULL, related_name='emi_plan', null=True, blank=True)
    down_payment = models.DecimalField(max_digits=12, decimal_places=2)
    loan_amount = models.DecimalField(max_digits=12, decimal_places=2)
    interest_rate = models.DecimalField(max_digits=5, decimal_places=2, help_text='Annual interest rate in %')
    tenure_months = models.PositiveIntegerField()
    monthly_emi = models.DecimalField(max_digits=12, decimal_places=2)
    total_interest = models.DecimalField(max_digits=12, decimal_places=2)
    total_payable = models.DecimalField(max_digits=12, decimal_places=2)
    plan_status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    start_date = models.DateField(auto_now_add=True)
    next_due_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.next_due_date:
            base_date = self.start_date or timezone.now().date()
            self.next_due_date = base_date + timedelta(days=30)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"EMI - {self.customer.name} - {self.car.name}"

    @property
    def final_due_date(self):
        """Return the approximate date when the last EMI is due."""
        if not self.start_date:
            return None
        return self.start_date + timedelta(days=30 * self.tenure_months)


class Sell(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)
    car = models.ForeignKey(Car, on_delete=models.CASCADE)
    car_color = models.ForeignKey('CarColor', on_delete=models.SET_NULL, null=True, blank=True, related_name='sells')
    sell_price = models.DecimalField(max_digits=12, decimal_places=2)
    sell_date = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"Sale - {self.car.name} to {self.customer.name}"

    class Meta:
        verbose_name = "Sale"
        verbose_name_plural = "Sales"


class Inquiry(models.Model):
    name = models.CharField(max_length=200)
    email = models.EmailField()
    phone = models.CharField(max_length=15)
    car = models.ForeignKey(Car, on_delete=models.CASCADE)
    message = models.TextField()
    is_resolved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"Inquiry - {self.name} - {self.car.name}"
    
    class Meta:
        verbose_name_plural = "Inquiries"


class CarReview(models.Model):
    RATING_CHOICES = [(i, str(i)) for i in range(1, 6)]

    car = models.ForeignKey(Car, on_delete=models.CASCADE, related_name='reviews')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='car_reviews')
    rating = models.PositiveSmallIntegerField(choices=RATING_CHOICES)
    title = models.CharField(max_length=120, blank=True)
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('car', 'user')
        ordering = ['-created_at']

    def __str__(self):
        return f"Review {self.rating}★ - {self.car.name} by {self.user.username}"


class FinanceSetting(models.Model):
    singleton_key = models.CharField(max_length=20, unique=True, editable=False, default='default')
    default_interest_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('8.50'), help_text='Default EMI interest rate applied for all customers (%)')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Finance Setting'
        verbose_name_plural = 'Finance Settings'

    def __str__(self):
        return "Finance Configuration"

    def save(self, *args, **kwargs):
        self.singleton_key = 'default'
        super().save(*args, **kwargs)

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(
            singleton_key='default',
            defaults={'default_interest_rate': Decimal('8.50')}
        )
        return obj


class ContactMessage(models.Model):
    SUBJECT_CHOICES = [
        ('general', 'General Inquiry'),
        ('purchase', 'Car Purchase'),
        ('test_drive', 'Test Drive'),
        ('feedback', 'Feedback'),
    ]
    
    name = models.CharField(max_length=100)
    email = models.EmailField()
    phone = models.CharField(max_length=20, blank=True)
    subject = models.CharField(max_length=50, choices=SUBJECT_CHOICES, default='general')
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.name} - {self.get_subject_display()}"
    
    class Meta:
        ordering = ['-created_at']


class TestDrive(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('confirmed', 'Confirmed'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]
    
    TIME_SLOTS = [
        ('09:00', '9:00 AM - 10:00 AM'),
        ('10:00', '10:00 AM - 11:00 AM'),
        ('11:00', '11:00 AM - 12:00 PM'),
        ('12:00', '12:00 PM - 1:00 PM'),
        ('14:00', '2:00 PM - 3:00 PM'),
        ('15:00', '3:00 PM - 4:00 PM'),
        ('16:00', '4:00 PM - 5:00 PM'),
        ('17:00', '5:00 PM - 6:00 PM'),
    ]
    
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='test_drives')
    car = models.ForeignKey(Car, on_delete=models.CASCADE, related_name='test_drives')
    car_color = models.ForeignKey('CarColor', on_delete=models.SET_NULL, null=True, blank=True, related_name='test_drives')
    full_name = models.CharField(max_length=100)
    email = models.EmailField()
    phone = models.CharField(max_length=20)
    preferred_date = models.DateField()
    preferred_time = models.CharField(max_length=10, choices=TIME_SLOTS)
    driving_license = models.CharField(max_length=50)
    address = models.TextField()
    message = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"{self.full_name} - {self.car.name} ({self.preferred_date})"
    
    class Meta:
        ordering = ['-created_at']


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    photo = models.ImageField(upload_to='profiles/', blank=True, null=True)
    bio = models.TextField(blank=True, max_length=500)
    phone = models.CharField(max_length=20, blank=True)
    
    # Soft delete fields
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)
    original_username = models.CharField(max_length=150, blank=True)
    original_email = models.EmailField(blank=True)
    
    def __str__(self):
        return f"{self.user.username}'s Profile"
    
    @property
    def photo_url(self):
        if self.photo:
            return self.photo.url
        return None
    
    def soft_delete(self):
        """Soft delete the user account - anonymize personal data but keep public contributions."""
        from django.utils import timezone
        
        # Store original data before anonymizing
        self.original_username = self.user.username
        self.original_email = self.user.email
        self.is_deleted = True
        self.deleted_at = timezone.now()
        
        # Count existing deleted users with same base username to create unique suffix
        deleted_count = UserProfile.objects.filter(
            original_username=self.user.username,
            is_deleted=True
        ).count()
        
        # Change username to "username(deleted_X)" format - keeps original username recognizable in admin
        self.user.username = f"{self.original_username}(deleted_{deleted_count + 1})"
        # Keep email unchanged for record purposes
        self.user.first_name = ""
        self.user.last_name = ""
        self.user.is_active = False  # Prevent login
        self.user.set_unusable_password()  # Remove password
        self.user.save()
        
        # Clear personal profile data
        self.bio = ""
        self.phone = ""
        if self.photo:
            self.photo.delete(save=False)
            self.photo = None
        self.save()
        
        return True
    
    @property
    def display_name(self):
        """Return display name - 'Deleted User' for deleted accounts, username otherwise."""
        if self.is_deleted:
            return "Deleted User"
        return self.user.username


class Notification(models.Model):
    NOTIFICATION_TYPES = [
        ('info', 'Information'),
        ('success', 'Success'),
        ('warning', 'Warning'),
        ('alert', 'Alert'),
        ('promo', 'Promotion'),
    ]
    
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications', null=True, blank=True)
    title = models.CharField(max_length=200)
    message = models.TextField()
    notification_type = models.CharField(max_length=20, choices=NOTIFICATION_TYPES, default='info')
    is_read = models.BooleanField(default=False)
    is_global = models.BooleanField(default=False, help_text='If true, notification is sent to all users')
    link = models.CharField(max_length=500, blank=True, help_text='Optional link for the notification')
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='sent_notifications')
    
    def __str__(self):
        if self.is_global:
            return f"[Global] {self.title}"
        return f"{self.title} - {self.user.username if self.user else 'All Users'}"
    
    class Meta:
        ordering = ['-created_at']


class NotificationRead(models.Model):
    """Track which global notifications have been read by which users"""
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    notification = models.ForeignKey(Notification, on_delete=models.CASCADE)
    read_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = ['user', 'notification']


# Signal to create profile automatically
from django.db.models.signals import post_save
from django.dispatch import receiver

@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.create(user=instance)

@receiver(post_save, sender=User)
def save_user_profile(sender, instance, **kwargs):
    if hasattr(instance, 'profile'):
        instance.profile.save()
    else:
        UserProfile.objects.create(user=instance)


class ContactInfo(models.Model):
    """Singleton model for store contact information."""
    singleton_key = models.CharField(max_length=20, unique=True, editable=False, default='default')
    address = models.TextField(help_text="Store address")
    phone_1 = models.CharField(max_length=20, help_text="Primary phone number")
    phone_2 = models.CharField(max_length=20, blank=True, help_text="Secondary phone number (optional)")
    email_1 = models.EmailField(help_text="Primary contact email")
    email_2 = models.EmailField(blank=True, help_text="Secondary contact email (optional)")
    working_hours = models.TextField(help_text="Working hours text (e.g., 'Mon - Sat: 9:00 AM - 7:00 PM')")
    map_embed_url = models.TextField(blank=True, help_text="Google Maps Embed URL (iframe src)")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Contact Information'
        verbose_name_plural = 'Contact Information'

    def __str__(self):
        return "Store Contact Details"

    def save(self, *args, **kwargs):
        self.singleton_key = 'default'
        super().save(*args, **kwargs)

    @classmethod
    def get_solo(cls):
        obj, created = cls.objects.get_or_create(
            singleton_key='default',
            defaults={
                'address': 'Ambernath (East)\nMumbai, Maharashtra - 421501',
                'phone_1': '+91 9975561028',
                'email_1': 'kamallochanlpradhan200624@gmail.com',
                'working_hours': 'Mon - Sat: 9:00 AM - 7:00 PM\nSunday: 10:00 AM - 2:00 PM'
            }
        )
        return obj
