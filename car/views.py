from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, HttpResponse
from django.contrib import messages
from django.contrib.auth import login, authenticate, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db.models import Sum, Count, Q, Avg
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from django.db import transaction
from django.core.paginator import Paginator
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings as django_settings
import re
import decimal
import calendar # Required for _add_months helper
from difflib import SequenceMatcher
import stripe

from .models import Car, CarColor, Customer, Payment, Sell, Inquiry, EMIPlan, CarReview, FinanceSetting, ContactMessage, TestDrive, Notification, NotificationRead, UserProfile, ContactInfo

# Initialize Stripe
stripe.api_key = django_settings.STRIPE_SECRET_KEY

DEFAULT_EMI_INTEREST = Decimal('8.5')
STAR_RANGE = range(1, 6)

# --- HELPER FUNCTIONS ---

def _get_default_interest_rate() -> Decimal:
    """Retrieves the default interest rate from FinanceSetting or uses the default constant."""
    try:
        # Assuming FinanceSetting is a singleton model (using get_solo)
        return FinanceSetting.get_solo().default_interest_rate
    except Exception:
        return DEFAULT_EMI_INTEREST


def _compute_emi(principal: Decimal, interest_rate: Decimal, tenure_months: int):
    """
    Calculates the EMI, total payable amount, and total interest using precise Decimal math.
    Return (monthly_emi, total_amount, total_interest).
    """
    principal = Decimal(principal).quantize(Decimal('0.01'))
    rate = Decimal(interest_rate)

    if tenure_months <= 0 or principal <= 0:
        raise ValueError('Invalid EMI parameters')
        
    monthly_rate = rate / Decimal('1200') # R / 100 / 12

    if monthly_rate > 0:
        factor = (Decimal('1') + monthly_rate) ** tenure_months
        # EMI Formula: P * r * (1+r)^n / ((1+r)^n - 1)
        emi = principal * monthly_rate * factor / (factor - Decimal('1'))
    else:
        # Zero interest rate is simple division
        emi = principal / tenure_months
        
    emi = emi.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    total_amount = (emi * tenure_months).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    total_interest = (total_amount - principal).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    
    return emi, total_amount, total_interest


def _add_months(sourcedate, months):
    """Adds a number of months to a date, handling month-end dates gracefully (for precise EMI due dates)."""
    month = sourcedate.month + months
    year = sourcedate.year + month // 12
    month = month % 12
    if month == 0:
        month = 12
        year -= 1
    day = min(sourcedate.day, calendar.monthrange(year, month)[1])
    return sourcedate.replace(year=year, month=month, day=day)


def get_time_ago(dt):
    """Get human readable time ago string"""
    now = timezone.now()
    diff = now - dt
    
    if diff.days > 7:
        return dt.strftime('%b %d')
    elif diff.days > 0:
        return f'{diff.days}d ago'
    elif diff.seconds > 3600:
        return f'{diff.seconds // 3600}h ago'
    elif diff.seconds > 60:
        return f'{diff.seconds // 60}m ago'
    else:
        return 'Just now'


# --- CORE VIEWS ---

def home(request):
    cars = (Car.objects.filter(is_available=True)
            .prefetch_related('colors', 'colors__images')
            .annotate(avg_rating=Avg('reviews__rating'), rating_count=Count('reviews'))[:6])
    total_cars = Car.objects.filter(is_available=True).count()
    
    # Build car list with color info
    car_list = []
    processed_car_ids = set()
    
    for car in cars:
        if car.id in processed_car_ids:
            continue
            
        processed_car_ids.add(car.id)
        car_colors = list(car.colors.all())
        
        # Select default color (first available)
        default_color = car_colors[0] if car_colors else None
        
        # Determine image URL
        image_url = car.image_src
        if default_color:
            color_img = default_color.images.first()
            if color_img:
                image_url = color_img.image_url
        
        car_list.append({
            'car': car,
            'color': default_color, # Default color object for initial display
            'display_name': f"{car.name} ({default_color.name})" if default_color else car.name,
            'image_url': image_url,
            'colors': car_colors,
        })
    
    # Real-time stats
    total_brands = Car.objects.filter(is_available=True).values('brand').distinct().count()
    avg_rating = CarReview.objects.aggregate(avg=Avg('rating'))['avg'] or 0
    avg_rating = round(avg_rating, 1) if avg_rating else 0
    total_customers = Customer.objects.count()
    
    # Get all distinct brands for the search dropdown
    brands = Car.objects.filter(is_available=True).values_list('brand', flat=True).distinct().order_by('brand')
    
    return render(request, 'home.html', {
        'cars': car_list, 
        'total_cars': total_cars,
        'star_range': STAR_RANGE,
        'total_brands': total_brands,
        'avg_rating': avg_rating,
        'total_customers': total_customers,
        'brands': brands,
    })


def car_list(request):
    cars = (Car.objects
            .filter(is_available=True)
            .only('id', 'name', 'brand', 'model_year', 'fuel_type', 'transmission',
                  'mileage', 'engine', 'price', 'image', 'is_available'))
    brands = Car.objects.filter(is_available=True).values_list('brand', flat=True).distinct().order_by('brand')
    
    # Filters
    brand = request.GET.get('brand')
    fuel_type = request.GET.get('fuel_type')
    min_price = request.GET.get('min_price')
    max_price = request.GET.get('max_price')
    search_query = request.GET.get('q', '').strip()
    
    if brand:
        cars = cars.filter(brand=brand)
    if fuel_type:
        cars = cars.filter(fuel_type=fuel_type)
    if min_price:
        cars = cars.filter(price__gte=min_price)
    if max_price:
        cars = cars.filter(price__lte=max_price)
    if search_query:
        # Improved search: handles multi-word queries with AND logic
        terms = [term.lower() for term in re.split(r"\s+", search_query) if term and len(term) > 1]
        
        if terms:
            # Strategy 1: Exact match - ALL terms must be found somewhere in the car data
            # Build a query that requires ALL terms to match
            all_terms_match = Q()
            for term in terms:
                term_match = (
                    Q(name__icontains=term) |
                    Q(brand__icontains=term) |
                    Q(description__icontains=term) |
                    Q(engine__icontains=term) |
                    Q(mileage__icontains=term) |
                    Q(fuel_type__icontains=term) |
                    Q(transmission__icontains=term) |
                    Q(colors__name__icontains=term)
                )
                all_terms_match &= term_match  # AND logic - all terms must match
            
            exact_cars = cars.filter(all_terms_match).distinct()
            
            # If strict AND gives results, use them; otherwise fall back to scoring
            if exact_cars.exists():
                cars = exact_cars
            else:
                # Strategy 2: Score-based matching - count how many terms match each car
                all_cars = list(cars.prefetch_related('colors'))
                scored_matches = []
                
                for car in all_cars:
                    # Build searchable text
                    color_names = " ".join([c.name for c in car.colors.all()])
                    searchable_text = f"{car.name} {car.brand} {car.description} {car.fuel_type} {car.transmission} {car.engine} {car.mileage} {color_names}".lower()
                    searchable_words = set(searchable_text.split())
                    
                    # Count matching terms
                    matched_terms = 0
                    total_score = 0
                    
                    for term in terms:
                        # Exact word match
                        if term in searchable_text:
                            matched_terms += 1
                            total_score += 1.0
                        else:
                            # Fuzzy match against each word
                            best_word_match = 0
                            for word in searchable_words:
                                if len(word) >= 2:
                                    similarity = SequenceMatcher(None, term, word).ratio()
                                    best_word_match = max(best_word_match, similarity)
                            
                            if best_word_match >= 0.75:  # Higher threshold for fuzzy
                                matched_terms += 1
                                total_score += best_word_match
                    
                    # Calculate match percentage
                    match_percentage = matched_terms / len(terms) if terms else 0
                    
                    # Only include if at least 70% of terms match (for multi-word queries)
                    # Or if single term and it has good match
                    min_match_ratio = 0.7 if len(terms) > 1 else 0.6
                    if match_percentage >= min_match_ratio:
                        scored_matches.append((car.id, total_score, match_percentage))
                
                # Sort by match percentage first, then by score
                scored_matches.sort(key=lambda x: (x[2], x[1]), reverse=True)
                matched_ids = [car_id for car_id, _, _ in scored_matches]
                
                if matched_ids:
                    from django.db.models import Case, When, IntegerField
                    preserved_order = Case(*[When(id=id, then=pos) for pos, id in enumerate(matched_ids)], output_field=IntegerField())
                    cars = cars.filter(id__in=matched_ids).order_by(preserved_order)
                else:
                    # No matches at all - return empty
                    cars = cars.none()
    
    cars = cars.annotate(avg_rating=Avg('reviews__rating'), rating_count=Count('reviews'))
    
    # Prefetch colors with availability info for Amazon-style display
    cars = cars.prefetch_related('colors', 'colors__images')
    
    # Paginate unique cars directly (no duplicates)
    paginator = Paginator(cars, 12)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # Wrap cars to match car_card.html expectations
    wrapped_cars = []
    
    # Pre-process search terms for color matching logic
    search_terms = []
    if search_query:
        search_terms = [t.lower() for t in search_query.split() if len(t) > 2] # Ignore very short words like 'car' potentially, or 'is'

    for car in page_obj:
        car_colors = list(car.colors.all())
        default_color = car_colors[0] if car_colors else None
        
        # SMART COLOR SELECTION: Check if any search term matches a color name
        if search_query and car_colors:
            for color in car_colors:
                color_name = color.name.lower()
                # Check for exact name match in terms OR term in color name
                # Priority to the first match found
                match_found = False
                for term in search_terms:
                    if term in color_name:
                        default_color = color
                        match_found = True
                        break
                if match_found:
                    break
        
        # Determine image URL
        image_url = car.image_src
        if default_color:
            color_img = default_color.images.first()
            if color_img:
                image_url = color_img.image_url
                
        wrapped_cars.append({
            'car': car,
            'color': default_color,
            'display_name': car.name,
            'image_url': image_url,
            'colors': car_colors,
        })

    query_params = request.GET.copy()
    query_params.pop('page', None)
    preserved_query = query_params.urlencode()
    
    return render(request, 'car_list.html', {
        'cars': wrapped_cars,
        'brands': brands,
        'search_query': search_query,
        'star_range': STAR_RANGE,
        'page_obj': page_obj,
        'paginator_query': preserved_query,
    })


def car_detail(request, car_id):
    car = get_object_or_404(
        Car.objects.annotate(avg_rating=Avg('reviews__rating'), rating_count=Count('reviews')),
        id=car_id
    )
    related_cars = Car.objects.filter(brand=car.brand, is_available=True).exclude(id=car_id)[:10]
    reviews = car.reviews.select_related('user').all()
    user_review = None
    if request.user.is_authenticated:
        user_review = reviews.filter(user=request.user).first()
    rating_breakdown = car.reviews.values('rating').annotate(total=Count('id')).order_by('-rating')
    
    # --- EMI Defaults and Sample Calculation ---
    sample_emi = None
    emi_tenure_months = 60
    price_amount = car.price_value # Assuming car.price_value is a Decimal
    
    try:
        if price_amount and price_amount > Decimal('0'):
            interest_rate = _get_default_interest_rate()
            # Calculate sample EMI assuming 0 down payment for simplicity on detail page
            sample_emi, _, _ = _compute_emi(price_amount, interest_rate, emi_tenure_months)
    except Exception:
        sample_emi = None
        
    # Set EMI Calculator slider defaults
    down_payment_min = (price_amount * Decimal('0.10')).quantize(Decimal('1'), rounding=ROUND_HALF_UP) if price_amount else Decimal('0')
    down_payment_max = (price_amount * Decimal('0.90')).quantize(Decimal('1'), rounding=ROUND_HALF_UP) if price_amount else Decimal('0')
    down_payment_default = (price_amount * Decimal('0.20')).quantize(Decimal('1'), rounding=ROUND_HALF_UP) if price_amount else Decimal('0')
    
    # Adjust edge cases for sliders
    if down_payment_max <= 0 and price_amount > 0:
        down_payment_max = price_amount - Decimal('1')
    if down_payment_default < down_payment_min:
        down_payment_default = down_payment_min
    if down_payment_max and down_payment_default > down_payment_max:
        down_payment_default = down_payment_max
        
    loan_amount_default = max(price_amount - down_payment_default, Decimal('0'))
    
    emi_defaults = {
        'down_payment_min': int(down_payment_min),
        'down_payment_max': int(down_payment_max) if down_payment_max > 0 else int(price_amount),
        'down_payment_default': int(down_payment_default),
        'loan_amount_default': int(loan_amount_default),
        'tenure_min': 12,
        'tenure_max': 84,
        'tenure_default': emi_tenure_months,
        'tenure_years_default': emi_tenure_months // 12,
        'interest_min': 6,
        'interest_max': 20,
        'interest_default': float(_get_default_interest_rate()),
    }
    
    # Get selected color from URL parameter
    selected_color_id = request.GET.get('color')
    
    return render(request, 'car_detail.html', {
        'car': car,
        'related_cars': related_cars,
        'reviews': reviews,
        'user_review': user_review,
        'rating_breakdown': rating_breakdown,
        'star_range': STAR_RANGE,
        'sample_emi': sample_emi,
        'emi_tenure_years': emi_tenure_months // 12,
        'emi_defaults': emi_defaults,
        'selected_color_id': selected_color_id,
    })


@login_required(login_url='login')
def inquiry(request, car_id):
    car = get_object_or_404(Car, id=car_id)
    
    # Get color from URL param
    color_id = request.GET.get('color')
    selected_color = None
    image_url = car.image_src
    
    if color_id:
        try:
            selected_color = car.colors.get(id=color_id)
            # Get color-specific image
            color_image = selected_color.images.first()
            if color_image:
                image_url = color_image.image_url
        except CarColor.DoesNotExist:
            pass
    
    # Fallback: if no image yet, try first color's image
    if not image_url:
        first_color = car.colors.first()
        if first_color:
            first_img = first_color.images.first()
            if first_img:
                image_url = first_img.image_url
                if not selected_color:
                    selected_color = first_color
    
    customer = None
    if hasattr(request.user, 'customer'):
        customer = request.user.customer
    
    if request.method == 'POST':
        name = request.POST.get('name')
        email = request.POST.get('email')
        phone = request.POST.get('phone')
        message = request.POST.get('message')
        
        Inquiry.objects.create(
            name=name,
            email=email,
            phone=phone,
            car=car,
            message=message
        )
        
        # Create notification for inquiry submission if user is logged in
        if request.user.is_authenticated:
            Notification.objects.create(
                user=request.user,
                title='✉️ Inquiry Submitted',
                message=f'Your inquiry about {car.brand} {car.name} has been submitted. Our team will respond within 24 hours.',
                notification_type='info',
                link=f'/car/{car.id}/',
                is_global=False
            )
        
        messages.success(request, 'Your inquiry has been submitted successfully!')
        return redirect('car_detail', car_id=car_id)
    
    return render(request, 'inquiry.html', {
        'car': car, 
        'customer': customer,
        'image_url': image_url,
        'selected_color': selected_color,
    })


@login_required(login_url='login')
def test_drive(request, car_id):
    car = get_object_or_404(Car, id=car_id)
    
    if not car.is_available:
        messages.error(request, 'This car is no longer available for test drive.')
        return redirect('car_detail', car_id=car_id)
    
    user = request.user
    
    # Get color from URL for initial load
    color_id_get = request.GET.get('color')
    car_color = None
    if color_id_get:
        car_color = CarColor.objects.filter(id=color_id_get, car=car).first()
    
    if request.method == 'POST':
        full_name = request.POST.get('full_name')
        email = request.POST.get('email')
        phone = request.POST.get('phone')
        preferred_date = request.POST.get('preferred_date')
        preferred_time = request.POST.get('preferred_time')
        driving_license = request.POST.get('driving_license')
        address = request.POST.get('address')
        message = request.POST.get('message', '')
        
        # Get color from hidden input
        color_id = request.POST.get('color')
        car_color = None
        if color_id:
            car_color = CarColor.objects.filter(id=color_id, car=car).first()
        
        existing = TestDrive.objects.filter(
            user=user, 
            car=car, 
            status__in=['pending', 'confirmed']
        ).exists()
        
        if existing:
            messages.warning(request, 'You already have a pending or confirmed test drive for this car.')
            return redirect('car_detail', car_id=car_id)
        
        test_drive_booking = TestDrive.objects.create(
            user=user,
            car=car,
            car_color=car_color,
            full_name=full_name,
            email=email,
            phone=phone,
            preferred_date=preferred_date,
            preferred_time=preferred_time,
            driving_license=driving_license,
            address=address,
            message=message
        )
        
        Notification.objects.create(
            user=user,
            title='🚗 Test Drive Booked!',
            message=f'Your test drive for {car.brand} {car.name} has been scheduled for {preferred_date} at {preferred_time}. Our team will contact you to confirm.',
            notification_type='success',
            link=f'/car/{car.id}/',
            is_global=False
        )
        
        return redirect('test_drive_confirmation', booking_id=test_drive_booking.id)
    
    from datetime import date, timedelta
    min_date = (date.today() + timedelta(days=1)).isoformat()
    max_date = (date.today() + timedelta(days=30)).isoformat()
    
    # Assuming TestDrive.TIME_SLOTS is defined in models.py
    time_slots = TestDrive.TIME_SLOTS 
    
    # Get user profile and customer for auto-fill
    user_profile = getattr(user, 'profile', None)
    customer = Customer.objects.filter(user=user).first()
    
    return render(request, 'test_drive.html', {
        'car': car,
        'car_color': car_color,
        'user': user,
        'user_profile': user_profile,
        'customer': customer,
        'min_date': min_date,
        'max_date': max_date,
        'time_slots': time_slots,
    })


@login_required(login_url='login')
def test_drive_confirmation(request, booking_id):
    """Test drive booking confirmation page"""
    booking = get_object_or_404(TestDrive, id=booking_id, user=request.user)
    return render(request, 'test_drive_confirmation.html', {
        'booking': booking,
        'car': booking.car,
    })


@login_required(login_url='login')
def make_payment(request, car_id):
    car = get_object_or_404(Car, id=car_id)
    
    # Handle color selection
    color_id = request.POST.get('color') or request.GET.get('color')
    car_color = None
    if color_id:
        car_color = CarColor.objects.filter(id=color_id, car=car).first()
        
    car_price_value = car.price_value
    
    customer = None
    if hasattr(request.user, 'customer'):
        customer = request.user.customer
    
    recommended_down_payment = (car_price_value * Decimal('0.2')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    minimum_down_payment = (car_price_value * Decimal('0.1')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    global_interest_rate = _get_default_interest_rate().quantize(Decimal('0.01'))
    
    # Assuming car.emi_interest_rate exists and is a DecimalField
    car_interest_rate = car.emi_interest_rate.quantize(Decimal('0.01')) if car.emi_interest_rate is not None else None
    default_interest_rate = car_interest_rate or global_interest_rate
    
    if request.method == 'POST':
        name = request.POST.get('name')
        email = request.POST.get('email')
        phone = request.POST.get('phone')
        address = request.POST.get('address')
        payment_method = request.POST.get('payment_method')
        payment_type = request.POST.get('payment_type', 'full')
        amount = Decimal(request.POST.get('amount', car_price_value))
        down_payment = Decimal(request.POST.get('down_payment', '0') or '0').quantize(Decimal('0.01'))
        
        # Verify color stock if selected
        if car_color and car_color.stock <= 0:
            messages.error(request, f'Selected color {car_color.name} is out of stock.')
            return redirect('car_detail', car_id=car_id)
        elif not car_color and car.stock <= 0:
             messages.error(request, 'Car is out of stock.')
             return redirect('car_detail', car_id=car_id)
        
        # Determine final interest rate
        if request.user.is_staff:
            interest_rate_value = request.POST.get('interest_rate')
            source_value = interest_rate_value if interest_rate_value not in (None, '') else default_interest_rate
            interest_rate = Decimal(source_value).quantize(Decimal('0.01'))
        else:
            interest_rate = default_interest_rate
            
        tenure_months = int(request.POST.get('emi_tenure', 36))
        emi_plan = None
        loan_amount = Decimal('0.00')
        monthly_emi = Decimal('0.00')
        total_interest = Decimal('0.00')
        total_amount = Decimal('0.00')
        
        if payment_type == 'emi':
            if down_payment < minimum_down_payment:
                messages.error(request, f'Minimum down payment for EMI is ₹{minimum_down_payment:,.0f} (10% of car price).')
                return redirect('make_payment', car_id=car_id)
            if down_payment >= car_price_value:
                messages.error(request, 'Down payment must be less than car price for EMI option.')
                return redirect('make_payment', car_id=car_id)
                
            loan_amount = (car_price_value - down_payment).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            
            try:
                monthly_emi, total_amount, total_interest = _compute_emi(loan_amount, interest_rate, tenure_months)
            except ValueError:
                messages.error(request, 'Invalid EMI parameters. Please try again.')
                return redirect('make_payment', car_id=car_id)
                
            amount = down_payment # The payment being made now is the down payment
            
        with transaction.atomic():
            # Update customer info
            if customer:
                customer.name = name
                customer.phone = phone
                customer.address = address
                customer.save()
            else:
                customer = Customer.objects.create(
                    user=request.user,
                    name=name,
                    email=email,
                    phone=phone,
                    address=address
                )
            
            # 1. Create Down Payment/Full Payment Record
            payment = Payment.objects.create(
                customer=customer,
                car=car,
                car_color=car_color,
                amount=amount,
                payment_method=payment_method,
                payment_status='completed',
                transaction_id=f"TXN{timezone.now().strftime('%Y%m%d%H%M%S')}"
            )
            
            # 2. Record the Sell
            Sell.objects.create(
                customer=customer,
                car=car,
                car_color=car_color,
                sell_price=car_price_value
            )
            
            # 3. Reduce Stock
            if car_color:
                if car_color.stock > 0:
                    car_color.stock -= 1
                    car_color.save()
            
            # Reduce global stock too
            if car.stock > 0:
                car.stock -= 1
            
            # 4. Mark Car as Sold if generic stock 0 (simplified logic)
            # Refined: Update is_available based on stock
            if car.stock <= 0:
                car.is_available = False
            car.save()
            
            # 5. Create EMI Plan (if applicable)
            if payment_type == 'emi':
                # Set next_due_date to 1 month from payment date (standard practice)
                first_due_date = _add_months(payment.payment_date.date(), 1)
                
                emi_plan = EMIPlan.objects.create(
                    customer=customer,
                    car=car,
                    car_color=car_color,
                    payment=payment, # Link to the down payment record
                    down_payment=down_payment,
                    loan_amount=loan_amount,
                    interest_rate=interest_rate,
                    tenure_months=tenure_months,
                    monthly_emi=monthly_emi,
                    total_interest=total_interest,
                    total_payable=(down_payment + total_amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                    start_date=payment.payment_date.date(),
                    next_due_date=first_due_date,
                    plan_status='active'
                )
        

            
            # --- Notifications and Redirect ---
            if emi_plan:
                messages.success(request, f'Down payment received! EMI plan activated. Transaction ID: {payment.transaction_id}')
                Notification.objects.create(
                    user=request.user,
                    title='📋 EMI Plan Activated!',
                    message=f'Your EMI plan for {car.brand} {car.name} has been activated. Monthly EMI: ₹{monthly_emi:,.0f} for {tenure_months} months. First payment due on {first_due_date.strftime("%d %b %Y")}.',
                    notification_type='info',
                    link=f'/emi-plan/{emi_plan.id}/',
                    is_global=False
                )
            else:
                messages.success(request, f'Payment successful! Transaction ID: {payment.transaction_id}')
                Notification.objects.create(
                    user=request.user,
                    title='🎉 Congratulations on Your New Car!',
                    message=f'You have successfully purchased {car.brand} {car.name}. Thank you for choosing us! Your transaction ID is {payment.transaction_id}.',
                    notification_type='success',
                    link=f'/payment-success/{payment.id}/',
                    is_global=False
                )
            
            return redirect('payment_success', payment_id=payment.id)
    
    return render(request, 'make_payment.html', {
        'car': car,
        'car_color': car_color,
        'customer': customer,
        'recommended_down_payment': recommended_down_payment,
        'minimum_down_payment': minimum_down_payment,
        'interest_rate_default': default_interest_rate,
        'can_edit_interest': request.user.is_staff,
        'interest_source': 'car' if car_interest_rate else 'global'
    })


@login_required(login_url='login')
def payment_success(request, payment_id):
    payment = get_object_or_404(Payment, id=payment_id)
    if not request.user.is_staff:
        payment_owner = getattr(payment.customer, 'user', None)
        if payment_owner != request.user:
            messages.error(request, 'You do not have access to that receipt.')
            return redirect('profile')
            
    # Assuming the EMIPlan model has a related_name='emi_plan' on Payment
    try:
        emi_plan = payment.emi_plan 
    except EMIPlan.DoesNotExist:
        emi_plan = None
        
    return render(request, 'payment_success.html', {'payment': payment, 'emi_plan': emi_plan})


@login_required(login_url='login')
def emi_plan_detail(request, plan_id):
    plan = get_object_or_404(EMIPlan, id=plan_id)
    if not request.user.is_staff:
        plan_owner = getattr(plan.customer, 'user', None)
        if plan_owner != request.user:
            messages.error(request, 'You do not have access to that EMI plan.')
            return redirect('profile')

    today = timezone.now().date()
    
    # Payments made *after* the down payment (i.e., actual installments)
    payments = Payment.objects.filter(
        customer=plan.customer,
        car=plan.car,
        payment_status='completed',
        payment_date__date__gte=plan.start_date if plan.start_date else today # Use start_date for filtering
    ).order_by('payment_date').exclude(id=plan.payment_id) # Exclude the initial down payment record
    
    # Calculate progress based on ACTUAL EMI payments made
    total_installment_amount_paid = Decimal('0.00')
    actual_emis_paid = 0
    
    for payment in payments:
        # Sum the amount paid towards installments
        total_installment_amount_paid += payment.amount
        # Calculate how many full EMIs this specific payment covers
        actual_emis_paid += int(payment.amount / plan.monthly_emi)
    
    completed_installments = min(actual_emis_paid, plan.tenure_months)
    remaining_installments = max(plan.tenure_months - completed_installments, 0)
    progress_percent = (completed_installments / plan.tenure_months * 100) if plan.tenure_months else 0
    
    # Total paid is down payment + actual installment payments received
    total_paid = plan.down_payment + total_installment_amount_paid
    
    remaining_balance = plan.total_payable - total_paid
    if remaining_balance < 0:
        remaining_balance = Decimal('0.00')

    context = {
        'plan': plan,
        'completed_installments': completed_installments,
        'remaining_installments': remaining_installments,
        'progress_percent': round(progress_percent, 1),
        'total_paid': total_paid.quantize(Decimal('0.01')),
        'remaining_balance': remaining_balance.quantize(Decimal('0.01')),
        'today': today,
        'payments': payments, # Installment payments
        'actual_emis_paid': actual_emis_paid,
        'total_installment_amount_paid': total_installment_amount_paid.quantize(Decimal('0.01'))
    }
    return render(request, 'emi_plan_detail.html', context)


@login_required(login_url='login')
def make_emi_payment(request):
    """Handle EMI payment processing - IMPROVED LOGIC for next_due_date"""
    
    plan_id = request.GET.get('plan_id')
    amount = request.GET.get('amount', '0')
    payment_method = request.GET.get('method', '')
    payment_type = request.GET.get('type', 'single') # 'single', 'multiple', 'full'
    
    try:
        plan = EMIPlan.objects.get(id=plan_id)
        plan_owner = getattr(plan.customer, 'user', None)
        
        if plan_owner != request.user and not request.user.is_staff:
            messages.error(request, 'You do not have access to that EMI plan.')
            return redirect('profile')
        
        if plan.plan_status != 'active':
            messages.error(request, 'This EMI plan is not active.')
            return redirect('emi_plan_detail', plan_id=plan_id)
        
        amount_decimal = Decimal(str(amount)).quantize(Decimal('0.01'))
        
        # --- Pre-check amount ---
        if amount_decimal < plan.monthly_emi and payment_type != 'full':
            messages.error(request, f'Payment amount must cover at least one full EMI of ₹{plan.monthly_emi:,.2f}.')
            return redirect('emi_plan_detail', plan_id=plan_id)

        # --- Start Transaction & Payment Creation ---
        with transaction.atomic():
            transaction_id = f'EMI-{plan_id}-{int(timezone.now().timestamp())}'
            payment = Payment.objects.create(
                customer=plan.customer,
                car=plan.car,
                amount=amount_decimal,
                payment_method=payment_method,
                payment_status='completed',
                transaction_id=transaction_id,
                payment_date=timezone.now()
            )
            
            today = timezone.now().date()
            emi_count = 0
            
            if payment_type == 'full':
                # Full final payment: simply mark as completed
                plan.plan_status = 'completed'
                plan.next_due_date = None
                emi_count = plan.tenure_months # Fictitious count for messaging
                
                success_message = (
                    f'✓ <strong>EMI Plan Completed!</strong><br>'
                    f'Payment of <strong>₹{amount_decimal:,.2f}</strong> received.<br>'
                    f'Status: <strong>All dues cleared</strong><br>'
                    f'Transaction ID: <strong>{transaction_id}</strong>'
                )
            else:
                # Regular or Multiple EMI Payment
                emi_count = int(amount_decimal / plan.monthly_emi)
                
                # Determine the date to advance from: use the current next_due_date
                # If next_due_date is NULL, it means the first EMI is due one month after start_date
                start_date_for_advance = plan.next_due_date if plan.next_due_date else _add_months(plan.start_date, 1)

                # Advance the date by the number of EMIs paid
                new_due_date = _add_months(start_date_for_advance, emi_count)
                plan.next_due_date = new_due_date
                
                # Check for completion (Total principal + interest)
                total_installments_paid = Payment.objects.filter(
                    customer=plan.customer,
                    car=plan.car,
                    payment_status='completed',
                    payment_date__date__gte=plan.start_date
                ).exclude(id=plan.payment_id).aggregate(total=Sum('amount'))['total'] or Decimal('0')
                
                total_loan_payable_amount = plan.loan_amount + plan.total_interest
                
                # If total installments paid (excluding down payment) >= total loan amount
                if total_installments_paid >= total_loan_payable_amount.quantize(Decimal('0.01')):
                    plan.plan_status = 'completed'
                    plan.next_due_date = None
                    success_message = (
                        f'✓ <strong>EMI Plan Completed!</strong><br>'
                        f'Amount: <strong>₹{amount_decimal:,.2f}</strong><br>'
                        f'All {plan.tenure_months} EMI(s) paid | Status: <strong>Completed</strong><br>'
                        f'Transaction ID: <strong>{transaction_id}</strong>'
                    )
                else:
                    success_message = (
                        f'✓ <strong>Payment Successful!</strong><br>'
                        f'Amount: <strong>₹{amount_decimal:,.2f}</strong><br>'
                        f'{emi_count} EMI(s) paid | Next Due: <strong>{new_due_date.strftime("%d %b %Y")}</strong><br>'
                        f'Transaction ID: <strong>{transaction_id}</strong>'
                    )
        
        # Save EMI plan with updated status
        plan.save()
        
        # Create notification for user with more details (transaction id, EMIs covered, next due, status)
        try:
            plan_owner_user = plan_owner or request.user
            receipt_link = f"/payment-success/{payment.id}/"
            Notification.objects.create(
                user=plan_owner_user,
                title='EMI Payment Received',
                message=f'Payment of ₹{amount_decimal:,.2f} received for {plan.car.brand} {plan.car.name}',
                notification_type='success',
                link=receipt_link,
                is_global=False,
                created_by=request.user
            )
        except Exception as e:
            pass 
        
        from django.contrib.messages import constants as messages_constants
        messages.add_message(request, messages_constants.SUCCESS, success_message, extra_tags='safe')
        
        # Redirect to the unified payment success receipt page so EMI payments
        # display the same transaction receipt as full purchase payments.
        return redirect('payment_success', payment_id=payment.id)
        
    except EMIPlan.DoesNotExist:
        messages.error(request, '❌ EMI plan not found. Please check and try again.')
        return redirect('profile')
    except (ValueError, decimal.InvalidOperation, TypeError) as e:
        messages.error(request, '❌ Invalid payment details. Please verify the amount and try again.')
        return redirect('profile')


@login_required(login_url='login')
def submit_review(request, car_id):
    car = get_object_or_404(Car, id=car_id)
    if request.method != 'POST':
        return redirect('car_detail', car_id=car_id)

    rating = request.POST.get('rating')
    title = request.POST.get('title', '').strip()
    comment = request.POST.get('comment', '').strip()

    try:
        rating_value = int(rating)
    except (TypeError, ValueError):
        messages.error(request, 'Please select a valid rating between 1 and 5 stars.')
        return redirect('car_detail', car_id=car_id)

    if rating_value < 1 or rating_value > 5:
        messages.error(request, 'Please select a valid rating between 1 and 5 stars.')
        return redirect('car_detail', car_id=car_id)

    CarReview.objects.update_or_create(
        car=car,
        user=request.user,
        defaults={
            'rating': rating_value,
            'title': title,
            'comment': comment,
        }
    )
    
    Notification.objects.create(
        user=request.user,
        title='⭐ Review Submitted',
        message=f'Thank you for reviewing {car.brand} {car.name}! Your {rating_value}-star review helps other buyers make informed decisions.',
        notification_type='success',
        link=f'/car/{car.id}/',
        is_global=False
    )
    
    messages.success(request, 'Thank you for reviewing this car!')
    return redirect('car_detail', car_id=car_id)


def user_register(request):
    if request.method == 'POST':
        username = request.POST.get('username')
        email = request.POST.get('email')
        password = request.POST.get('password')
        name = request.POST.get('name')
        phone = request.POST.get('phone')
        
        if User.objects.filter(username=username).exists():
            messages.error(request, 'Username already exists')
            return redirect('register')
        
        user = User.objects.create_user(username=username, email=email, password=password)
        Customer.objects.create(user=user, name=name, email=email, phone=phone, address='')
        
        Notification.objects.create(
            user=user,
            title='🎉 Welcome to Car Store!',
            message=f'Hi {name}! Welcome to Car Store. Explore our wide range of cars, book test drives, and find your dream car today!',
            notification_type='success',
            link='/cars/',
            is_global=False
        )
        
        login(request, user)
        messages.success(request, 'Registration successful!')
        return redirect('home')
    
    return render(request, 'register.html')


def user_login(request):
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        
        user = authenticate(request, username=username, password=password)
        if user:
            login(request, user)
            messages.success(request, 'Login successful!')
            next_url = request.GET.get('next') or request.POST.get('next') or 'home'
            return redirect(next_url)
        else:
            messages.error(request, 'Invalid credentials')
    
    return render(request, 'login.html')


def user_logout(request):
    logout(request)
    messages.success(request, 'Logged out successfully!')
    return redirect('home')


@login_required(login_url='login')
def profile(request):
    """View user profile"""
    customer = None
    if hasattr(request.user, 'customer'):
        customer = request.user.customer
    
    payments = Payment.objects.filter(customer=customer).order_by('-payment_date') if customer else []
    inquiries = Inquiry.objects.filter(email=request.user.email).order_by('-created_at')[:5]
    emi_plans = EMIPlan.objects.filter(customer=customer).order_by('next_due_date') if customer else []
    test_drives = TestDrive.objects.filter(user=request.user).order_by('-preferred_date', '-preferred_time')
    sells = Sell.objects.filter(customer=customer).order_by('-sell_date') if customer else []
    
    return render(request, 'profile.html', {
        'customer': customer,
        'payments': payments,
        'inquiries': inquiries,
        'emi_plans': emi_plans,
        'test_drives': test_drives,
        'sells': sells,
        'today': timezone.now().date()
    })


@login_required(login_url='login')
def edit_profile(request):
    """Edit user profile with validation for unique fields"""
    customer = None
    if hasattr(request.user, 'customer'):
        customer = request.user.customer
    
    if request.method == 'POST':
        name = request.POST.get('name')
        username = request.POST.get('username')
        email = request.POST.get('email')
        phone = request.POST.get('phone')
        address = request.POST.get('address')
        
        errors = []
        
        if username != request.user.username:
            if User.objects.filter(username=username).exists():
                errors.append('Username already exists. Please choose a different username.')
        
        if email != request.user.email:
            if User.objects.filter(email=email).exists():
                errors.append('Email already registered. Please use a different email.')
        
        if customer and phone != customer.phone:
            if Customer.objects.filter(phone=phone).exclude(id=customer.id).exists():
                errors.append('Phone number already registered. Please use a different phone number.')
        elif not customer:
            if Customer.objects.filter(phone=phone).exists():
                errors.append('Phone number already registered. Please use a different phone number.')
        
        if errors:
            for error in errors:
                messages.error(request, error)
            return render(request, 'edit_profile.html', {'customer': customer})
        
        request.user.username = username
        request.user.email = email
        request.user.save()
        
        if customer:
            customer.name = name
            customer.email = email
            customer.phone = phone
            customer.address = address
            customer.save()
        else:
            Customer.objects.create(
                user=request.user,
                name=name,
                email=email,
                phone=phone,
                address=address
            )
        
        messages.success(request, 'Profile updated successfully!')
        return redirect('profile')
    
    return render(request, 'edit_profile.html', {'customer': customer})


@login_required(login_url='login')
def delete_account(request):
    """Soft delete user account with confirmation - only for non-staff users.
    Keeps public data like reviews but anonymizes personal information."""
    
    if request.user.is_staff:
        messages.error(request, 'Staff accounts cannot be deleted through this interface.')
        return redirect('profile')
    
    pending_payments = []
    active_emis = []
    can_delete = True
    
    try:
        customer = Customer.objects.get(user=request.user)
        
        pending_payments = Payment.objects.filter(customer=customer, payment_status='pending')
        active_emis = EMIPlan.objects.filter(customer=customer, plan_status='active')
        
        if pending_payments.exists() or active_emis.exists():
            can_delete = False
            
    except Customer.DoesNotExist:
        customer = None
    
    if request.method == 'POST':
        if not can_delete:
            messages.error(request, 'Cannot delete account. You have pending payments or active EMI plans.')
            return render(request, 'delete_account.html', {
                'can_delete': can_delete,
                'pending_payments': pending_payments,
                'active_emis': active_emis
            })
        
        confirmation = request.POST.get('confirmation')
        password = request.POST.get('password')
        
        if not request.user.check_password(password):
            messages.error(request, 'Incorrect password. Account deletion cancelled.')
            return render(request, 'delete_account.html', {
                'can_delete': can_delete,
                'pending_payments': pending_payments,
                'active_emis': active_emis
            })
        
        if confirmation != 'DELETE':
            messages.error(request, 'Confirmation text does not match. Please type DELETE to confirm.')
            return render(request, 'delete_account.html', {
                'can_delete': can_delete,
                'pending_payments': pending_payments,
                'active_emis': active_emis
            })
        
        # --- Soft delete process ---
        username = request.user.username
        
        # Delete private data
        Notification.objects.filter(user=request.user).delete()
        NotificationRead.objects.filter(user=request.user).delete()
        TestDrive.objects.filter(user=request.user).delete()
        
        if customer:
            Inquiry.objects.filter(email=customer.email).delete()
            
            # Anonymize customer record
            customer.name = "Deleted User"
            customer.email = f"deleted_{customer.id}@{timezone.now().strftime('%Y%m%d%H%M%S')}.local" # Anonymize email to free up address
            customer.phone = ""
            customer.address = "Account Deleted"
            customer.save()
        
        # Soft delete the user profile (anonymize personal data)
        try:
            # Assuming user_profile.soft_delete() handles deactivation and username/email anonymization
            user_profile = request.user.profile
            user_profile.soft_delete() 
        except UserProfile.DoesNotExist:
            # Fallback: deactivate the Django User and disable login
            request.user.is_active = False
            request.user.set_unusable_password()
            request.user.username = f"deleted_{request.user.id}_{timezone.now().strftime('%Y%m%d%H%M%S')}"
            request.user.email = f"deleted_{request.user.id}@{timezone.now().strftime('%Y%m%d%H%M%S')}.local"
            request.user.save()
        
        from django.contrib.auth import logout
        logout(request)
        
        messages.success(request, f'Account "{username}" has been deleted. Your reviews will remain visible as "Deleted User".')
        return redirect('home')
    
    return render(request, 'delete_account.html', {
        'can_delete': can_delete,
        'pending_payments': pending_payments,
        'active_emis': active_emis
    })


def about(request):
    from django.contrib.auth.models import User
    from django.db.models import Avg
    
    admins = User.objects.filter(is_superuser=True, is_active=True).order_by('date_joined')
    
    # Real-time stats
    cars_sold = Sell.objects.count()
    total_customers = Customer.objects.count()
    total_cars = Car.objects.count()
    avg_rating = CarReview.objects.aggregate(avg=Avg('rating'))['avg'] or 0
    happy_customer_percent = round((avg_rating / 5) * 100) if avg_rating else 98
    
    return render(request, 'about.html', {
        'admins': admins,
        'cars_sold': cars_sold,
        'total_customers': total_customers,
        'total_cars': total_cars,
        'happy_customer_percent': happy_customer_percent,
    })


def contact(request):
    if request.method == 'POST':
        subject_map = {
            'General Inquiry': 'general',
            'Car Purchase': 'purchase',
            'Test Drive': 'test_drive',
            'Feedback': 'feedback',
        }
        ContactMessage.objects.create(
            name=request.POST.get('name', ''),
            email=request.POST.get('email', ''),
            phone=request.POST.get('phone', ''),
            subject=subject_map.get(request.POST.get('subject', ''), 'general'),
            message=request.POST.get('message', '')
        )
        messages.success(request, 'Thank you for contacting us! We will get back to you soon.')
        return redirect('contact')
    contact_info = ContactInfo.get_solo()
    return render(request, 'contact.html', {'contact_info': contact_info})


def calculate_emi(request):
    """Calculate EMI for car loan (API endpoint)"""
    if request.method == 'POST':
        try:
            principal = Decimal(request.POST.get('principal', 0))
            rate_input = request.POST.get('rate')
            
            if rate_input is None or rate_input == '':
                rate = _get_default_interest_rate()
            else:
                rate = Decimal(rate_input)
                
            if rate <= 0:
                rate = _get_default_interest_rate()
            
            rate = max(Decimal('1'), min(rate, Decimal('30')))
            tenure = int(request.POST.get('tenure', 12))
            down_payment = Decimal(request.POST.get('down_payment', 0))
            loan_amount = principal - down_payment
            
            if loan_amount <= 0:
                return JsonResponse({'error': 'Loan amount must be greater than 0'}, status=400)
                
            emi, total_amount, total_interest = _compute_emi(loan_amount, rate, tenure)
            
            return JsonResponse({
                'emi': float(emi),
                'total_amount': float(total_amount),
                'total_interest': float(total_interest),
                'loan_amount': float(loan_amount),
                'down_payment': float(down_payment),
                'tenure': tenure,
                'rate': float(rate)
            })
        except (ValueError, TypeError) as e:
            return JsonResponse({'error': 'Invalid input values'}, status=400)
    
    return JsonResponse({'error': 'POST method required'}, status=405)


# --- API ENDPOINTS FOR CHARTS ---

def api_sales_data(request):
    today = timezone.now().date()
    last_6_months = today - timedelta(days=180)
    
    sells = Sell.objects.filter(sell_date__gte=last_6_months)
    monthly_sells = {}
    
    for sell in sells:
        month = sell.sell_date.strftime('%B %Y')
        if month in monthly_sells:
            monthly_sells[month] += float(sell.sell_price)
        else:
            monthly_sells[month] = float(sell.sell_price)
    
    return JsonResponse({
        'labels': list(monthly_sells.keys()),
        'data': list(monthly_sells.values())
    })


def api_brand_distribution(request):
    brand_counts = Car.objects.values('brand').annotate(count=Count('id'))
    return JsonResponse({
        'labels': [item['brand'] for item in brand_counts],
        'data': [item['count'] for item in brand_counts]
    })


# --- NOTIFICATION VIEWS ---

def get_user_notifications(user):
    """Get notifications for a user (only personal, excluding all global)."""
    if not user.is_authenticated:
        return []
    return Notification.objects.filter(user=user, is_global=False).order_by('-created_at')[:20]


@login_required(login_url='login')
def notifications_list(request):
    """View all notifications"""
    check_and_create_emi_notifications(request.user)
    
    personal = Notification.objects.filter(user=request.user, is_global=False).order_by('-created_at')
    all_notifications = list(personal)
    
    # Check if there are any unread notifications
    has_unread = Notification.objects.filter(user=request.user, is_global=False, is_read=False).exists()

    return render(request, 'notifications.html', {
        'notifications': all_notifications,
        'has_unread': has_unread,
    })


@login_required(login_url='login')
def mark_notification_read(request, notification_id):
    """Mark a notification as read"""
    notification = get_object_or_404(Notification, id=notification_id)
    
    if notification.user == request.user:
        notification.is_read = True
        notification.save()
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True})
    return redirect('notifications_list')


@login_required(login_url='login')
def mark_all_notifications_read(request):
    """Mark all notifications as read"""
    Notification.objects.filter(user=request.user, is_global=False, is_read=False).update(is_read=True)
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True})
    return redirect('notifications_list')


def api_notifications(request):
    """API endpoint to get notifications for navbar dropdown"""
    if not request.user.is_authenticated:
        return JsonResponse({'notifications': [], 'unread_count': 0})
    
    check_and_create_emi_notifications(request.user)
    
    notifications = get_user_notifications(request.user)
    
    unread_count = Notification.objects.filter(user=request.user, is_global=False, is_read=False).count()
    
    notifications_data = []
    for notif in notifications[:10]: 
        is_read = notif.is_read
        notifications_data.append({
            'id': notif.id,
            'title': notif.title,
            'message': notif.message[:100] + '...' if len(notif.message) > 100 else notif.message,
            'type': notif.notification_type,
            'link': notif.link,
            'is_read': is_read,
            'is_global': False,
            'created_at': notif.created_at.strftime('%b %d, %Y %I:%M %p'),
            'time_ago': get_time_ago(notif.created_at),
        })
    
    return JsonResponse({
        'notifications': notifications_data,
        'unread_count': unread_count
    })


def check_and_create_emi_notifications(user):
    """Check for upcoming EMI payments and create notifications if needed"""
    if not user.is_authenticated or not hasattr(user, 'customer'):
        return
    
    customer = user.customer
    today = timezone.now().date()
    
    active_plans = EMIPlan.objects.filter(customer=customer, plan_status='active')
    
    for plan in active_plans:
        if not plan.next_due_date:
            continue
        
        days_until_due = (plan.next_due_date - today).days
        
        # Upcoming payment (3 days before)
        if 0 < days_until_due <= 3:
            existing = Notification.objects.filter(
                user=user,
                title__contains=f'EMI Payment Due',
                message__contains=plan.car.name,
                created_at__date__gte=today - timedelta(days=3)
            ).exists()
            
            if not existing:
                Notification.objects.create(
                    user=user,
                    title=f'📅 EMI Payment Due Soon',
                    message=f'Your EMI payment of ₹{plan.monthly_emi:,.0f} for {plan.car.brand} {plan.car.name} is due on {plan.next_due_date.strftime("%d %b %Y")}. Please ensure timely payment.',
                    notification_type='warning',
                    link=f'/emi-plan/{plan.id}/',
                    is_global=False
                )
        
        # Overdue payment
        elif days_until_due < 0:
            days_overdue = abs(days_until_due)
            existing = Notification.objects.filter(
                user=user,
                title__contains='EMI Payment Overdue',
                message__contains=plan.car.name,
                created_at__date=today
            ).exists()
            
            if not existing:
                Notification.objects.create(
                    user=user,
                    title=f'⚠️ EMI Payment Overdue',
                    message=f'Your EMI payment of ₹{plan.monthly_emi:,.0f} for {plan.car.brand} {plan.car.name} is overdue by {days_overdue} day(s). Please pay immediately to avoid penalties.',
                    notification_type='alert',
                    link=f'/emi-plan/{plan.id}/',
                    is_global=False
                )
        
        # Payment due today
        elif days_until_due == 0:
            existing = Notification.objects.filter(
                user=user,
                title__contains='EMI Payment Due Today',
                message__contains=plan.car.name,
                created_at__date=today
            ).exists()
            
            if not existing:
                Notification.objects.create(
                    user=user,
                    title=f'🔔 EMI Payment Due Today',
                    message=f'Your EMI payment of ₹{plan.monthly_emi:,.0f} for {plan.car.brand} {plan.car.name} is due today. Pay now to avoid late fees.',
                    notification_type='alert',
                    link=f'/emi-plan/{plan.id}/',
                    is_global=False
                )


# --- Custom Error Pages ---

def error_404(request, exception):
    """Custom 404 error page"""
    return render(request, '404.html', status=404)


def error_500(request):
    """Custom 500 error page"""
    return render(request, '500.html', status=500)


# --- STRIPE PAYMENT INTEGRATION ---

@login_required(login_url='login')
def create_stripe_checkout(request, car_id):
    """Create a Stripe Checkout Session and redirect to Stripe's hosted page"""
    if request.method != 'POST':
        messages.error(request, 'Invalid request method.')
        return redirect('make_payment', car_id=car_id)
    
    car = get_object_or_404(Car, id=car_id)
    
    # Get form data
    name = request.POST.get('name')
    email = request.POST.get('email')
    phone = request.POST.get('phone')
    address = request.POST.get('address')
    color_id = request.POST.get('color')
    payment_type = request.POST.get('payment_type', 'full')
    
    # Get or create customer
    customer = None
    if hasattr(request.user, 'customer'):
        customer = request.user.customer
        customer.name = name
        customer.phone = phone
        customer.address = address
        customer.save()
    else:
        customer = Customer.objects.create(
            user=request.user,
            name=name,
            email=email,
            phone=phone,
            address=address
        )
    
    # Handle color selection
    car_color = None
    if color_id:
        car_color = CarColor.objects.filter(id=color_id, car=car).first()
    
    # Calculate amount based on payment type
    car_price = car.price_value
    if payment_type == 'emi':
        down_payment = Decimal(request.POST.get('down_payment', '0') or '0')
        amount = down_payment
        product_name = f"{car.brand} {car.name} - Down Payment (EMI)"
    else:
        amount = car_price
        product_name = f"{car.brand} {car.name} - Full Payment"
    
    # Convert to paise (Stripe uses smallest currency unit)
    amount_paise = int(amount * 100)
    
    try:
        # Store payment info in session for later
        request.session['stripe_payment_data'] = {
            'car_id': car_id,
            'color_id': color_id,
            'customer_id': customer.id,
            'amount': str(amount),
            'payment_type': payment_type,
            'down_payment': request.POST.get('down_payment', '0'),
            'emi_tenure': request.POST.get('emi_tenure', '36'),
        }
        
        # Create Stripe Checkout Session
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'inr',
                    'product_data': {
                        'name': product_name,
                        'description': f"{car.model_year} | {car.get_fuel_type_display()} | {car.get_transmission_display()}",
                    },
                    'unit_amount': amount_paise,
                },
                'quantity': 1,
            }],
            mode='payment',
            customer_email=email,
            success_url=request.build_absolute_uri('/stripe/success/') + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=request.build_absolute_uri('/stripe/cancel/'),
            metadata={
                'car_id': car_id,
                'customer_id': customer.id,
                'color_id': color_id or '',
                'payment_type': payment_type,
            }
        )
        
        # Redirect to Stripe Checkout
        return redirect(checkout_session.url)
        
    except stripe.error.StripeError as e:
        messages.error(request, f'Payment error: {str(e)}')
        return redirect('make_payment', car_id=car_id)
    except Exception as e:
        messages.error(request, f'An error occurred: {str(e)}')
        return redirect('make_payment', car_id=car_id)


@login_required(login_url='login')
def stripe_success(request):
    """Handle successful Stripe payment redirect"""
    session_id = request.GET.get('session_id')
    
    if not session_id:
        messages.error(request, 'Invalid payment session.')
        return redirect('home')
    
    try:
        # Retrieve the session from Stripe
        session = stripe.checkout.Session.retrieve(session_id)
        
        if session.payment_status != 'paid':
            messages.error(request, 'Payment was not completed.')
            return redirect('home')
        
        # Get payment data from session
        payment_data = request.session.get('stripe_payment_data', {})
        
        car_id = payment_data.get('car_id') or session.metadata.get('car_id')
        customer_id = payment_data.get('customer_id') or session.metadata.get('customer_id')
        color_id = payment_data.get('color_id') or session.metadata.get('color_id')
        payment_type = payment_data.get('payment_type', 'full')
        
        if not car_id or not customer_id:
            messages.error(request, 'Missing payment information.')
            return redirect('home')
        
        car = get_object_or_404(Car, id=car_id)
        customer = get_object_or_404(Customer, id=customer_id)
        
        car_color = None
        if color_id:
            car_color = CarColor.objects.filter(id=color_id, car=car).first()
        
        # Check if payment already processed (prevent duplicate processing)
        existing_payment = Payment.objects.filter(stripe_session_id=session_id).first()
        if existing_payment:
            return redirect('payment_success', payment_id=existing_payment.id)
        
        amount = Decimal(payment_data.get('amount', str(car.price_value)))
        
        with transaction.atomic():
            # Create Payment record
            payment = Payment.objects.create(
                customer=customer,
                car=car,
                car_color=car_color,
                amount=amount,
                payment_method='stripe',
                payment_status='completed',
                transaction_id=session.payment_intent,
                stripe_session_id=session_id
            )
            
            # Record the sell
            Sell.objects.create(
                customer=customer,
                car=car,
                car_color=car_color,
                sell_price=car.price_value
            )
            
            # Update stock
            if car_color and car_color.stock > 0:
                car_color.stock -= 1
                car_color.save()
            
            if car.stock > 0:
                car.stock -= 1
            if car.stock <= 0:
                car.is_available = False
            car.save()
            
            # Handle EMI plan if applicable
            if payment_type == 'emi':
                down_payment = Decimal(payment_data.get('down_payment', '0'))
                tenure_months = int(payment_data.get('emi_tenure', 36))
                interest_rate = _get_default_interest_rate()
                loan_amount = car.price_value - down_payment
                
                if loan_amount > 0:
                    monthly_emi, total_amount, total_interest = _compute_emi(loan_amount, interest_rate, tenure_months)
                    first_due_date = _add_months(payment.payment_date.date(), 1)
                    
                    emi_plan = EMIPlan.objects.create(
                        customer=customer,
                        car=car,
                        car_color=car_color,
                        payment=payment,
                        down_payment=down_payment,
                        loan_amount=loan_amount,
                        interest_rate=interest_rate,
                        tenure_months=tenure_months,
                        monthly_emi=monthly_emi,
                        total_interest=total_interest,
                        total_payable=(down_payment + total_amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                        start_date=payment.payment_date.date(),
                        next_due_date=first_due_date,
                        plan_status='active'
                    )
                    
                    Notification.objects.create(
                        user=request.user,
                        title='📋 EMI Plan Activated!',
                        message=f'Your EMI plan for {car.brand} {car.name} has been activated via Stripe. Monthly EMI: ₹{monthly_emi:,.0f} for {tenure_months} months.',
                        notification_type='info',
                        link=f'/emi-plan/{emi_plan.id}/',
                        is_global=False
                    )
            else:
                # Full payment notification
                Notification.objects.create(
                    user=request.user,
                    title='🎉 Congratulations on Your New Car!',
                    message=f'You have successfully purchased {car.brand} {car.name} via Stripe. Transaction ID: {payment.transaction_id}',
                    notification_type='success',
                    link=f'/payment-success/{payment.id}/',
                    is_global=False
                )
        
        # Clear session data
        if 'stripe_payment_data' in request.session:
            del request.session['stripe_payment_data']
        
        messages.success(request, f'Payment successful! Transaction ID: {payment.transaction_id}')
        return redirect('payment_success', payment_id=payment.id)
        
    except stripe.error.StripeError as e:
        messages.error(request, f'Error verifying payment: {str(e)}')
        return redirect('home')
    except Exception as e:
        messages.error(request, f'An error occurred: {str(e)}')
        return redirect('home')


def stripe_cancel(request):
    """Handle cancelled Stripe payment"""
    # Clear session data
    if 'stripe_payment_data' in request.session:
        payment_data = request.session.get('stripe_payment_data', {})
        car_id = payment_data.get('car_id')
        del request.session['stripe_payment_data']
        
        if car_id:
            color_id = payment_data.get('color_id')
            messages.warning(request, 'Payment was cancelled. You can try again when ready.')
            if color_id:
                return redirect(f'/car/{car_id}/payment/?color={color_id}')
            return redirect('make_payment', car_id=car_id)
    
    messages.warning(request, 'Payment was cancelled.')
    return redirect('home')


@login_required(login_url='login')
def create_emi_stripe_checkout(request, plan_id):
    """Create a Stripe Checkout Session for EMI payment"""
    if request.method != 'POST':
        messages.error(request, 'Invalid request method.')
        return redirect('emi_plan_detail', plan_id=plan_id)
    
    plan = get_object_or_404(EMIPlan, id=plan_id)
    if plan.plan_status != 'active':
        messages.error(request, 'This EMI plan is not active.')
        return redirect('emi_plan_detail', plan_id=plan_id)
        
    payment_type = request.POST.get('payment_type', 'single')
    amount_decimal = plan.monthly_emi
    emi_count = 1
    
    if payment_type == 'multiple':
        try:
            emi_count = int(request.POST.get('emi_count', '1'))
            if emi_count < 1: emi_count = 1
            if emi_count > (plan.tenure_months - plan.completed_installments_count):
                 # Logic for capping installments can be complex, skipping strict cap for now
                 pass
            amount_decimal = plan.monthly_emi * emi_count
        except ValueError:
            pass
    elif payment_type == 'full':
        # Calculate remaining balance
        # We need to recalculate accurately
        total_paid_so_far = Payment.objects.filter(
            customer=plan.customer, 
            car=plan.car, 
            payment_status='completed',
            payment_date__date__gte=plan.start_date
        ).aggregate(sum=Sum('amount'))['sum'] or Decimal('0')
        
        # Don't double count the down payment if it's already in total_paid_so_far
        # (Usually down payment is separate Payment record linked to plan, but let's be safe)
        # Actually remaining balance = Total Payable - Total Paid
        remaining = plan.total_payable - total_paid_so_far
        if remaining < 0: remaining = Decimal('0')
        amount_decimal = remaining
        
    amount_paise = int(amount_decimal * 100)
    
    product_name = f"EMI Payment - {plan.car.brand} {plan.car.name}"
    description = f"{payment_type.title()} Payment ({emi_count if payment_type == 'multiple' else 1} installments)"
    if payment_type == 'full':
        description = "Full Outstanding Balance Clearance"

    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'inr',
                    'product_data': {
                        'name': product_name,
                        'description': description,
                    },
                    'unit_amount': amount_paise,
                },
                'quantity': 1,
            }],
            mode='payment',
            customer_email=plan.customer.email,
            success_url=request.build_absolute_uri('/stripe/emi-success/') + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=request.build_absolute_uri(f'/emi-plan/{plan.id}/'),
            metadata={
                'plan_id': plan.id,
                'payment_type': payment_type,
                'emi_count': str(emi_count),
                'amount': str(amount_decimal)
            }
        )
        return redirect(checkout_session.url)
        
    except Exception as e:
        messages.error(request, f'Error creating payment session: {str(e)}')
        return redirect('emi_plan_detail', plan_id=plan_id)


@login_required(login_url='login')
def stripe_emi_success(request):
    """Handle successful Stripe EMI payment callback"""
    session_id = request.GET.get('session_id')
    if not session_id:
        return redirect('profile')
        
    try:
        session = stripe.checkout.Session.retrieve(session_id)
        if session.payment_status != 'paid':
             messages.error(request, 'Payment not completed.')
             return redirect('profile')
             
        plan_id = session.metadata.get('plan_id')
        payment_type = session.metadata.get('payment_type')
        amount_decimal = Decimal(session.metadata.get('amount'))
        
        plan = get_object_or_404(EMIPlan, id=plan_id)
        
        # Check if transaction already recorded to avoid duplicates
        if Payment.objects.filter(stripe_session_id=session_id).exists():
             payment = Payment.objects.filter(stripe_session_id=session_id).first()
             return redirect('payment_success', payment_id=payment.id)
             
        with transaction.atomic():
            payment = Payment.objects.create(
                customer=plan.customer,
                car=plan.car,
                amount=amount_decimal,
                payment_method='stripe',
                payment_status='completed',
                transaction_id=session.payment_intent,
                stripe_session_id=session_id,
                payment_date=timezone.now()
            )
            
            # Update Plan Status Logic (Copied/Adapted from make_emi_payment)
            if payment_type == 'full':
                plan.plan_status = 'completed'
                plan.next_due_date = None
            else:
                # Calculate emi count paid
                emi_count = int(amount_decimal / plan.monthly_emi)
                
                # Advance due date
                start_date_for_advance = plan.next_due_date if plan.next_due_date else _add_months(plan.start_date, 1)
                new_due_date = _add_months(start_date_for_advance, emi_count)
                plan.next_due_date = new_due_date
                
                # Check for total completion
                total_paid = Payment.objects.filter(
                    customer=plan.customer,
                    car=plan.car,
                    payment_status='completed',
                    payment_date__date__gte=plan.start_date
                ).exclude(id=plan.payment_id).aggregate(sum=Sum('amount'))['sum'] or Decimal('0')
                
                total_loan_repayment = plan.loan_amount + plan.total_interest
                if total_paid >= total_loan_repayment.quantize(Decimal('0.01')):
                    plan.plan_status = 'completed'
                    plan.next_due_date = None
            
            plan.save()
            
            # Notification
            Notification.objects.create(
                user=request.user,
                title='EMI Payment Received',
                message=f'EMI Payment of ₹{amount_decimal:,.2f} received via Stripe.',
                notification_type='success',
                link=f'/payment-success/{payment.id}/',
                is_global=False
            )
            
            messages.success(request, f'EMI payment successful! Transaction: {payment.transaction_id}')
            return redirect('payment_success', payment_id=payment.id)
            
    except Exception as e:
        messages.error(request, f"Error processing payment: {str(e)}")
        return redirect('profile')