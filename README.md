Full setup (run these in order):

Pakage installation:

    pip install django whitenoise stripe pillow
  
Database setup:

    python manage.py makemigrations
    python manage.py migrate
    
(options) Load sample data:

    python manage.py import_cars_from_csv
    python manage.py import_images_from_folder
    
Create superuser:

    python manage.py createsuperuser
    
Run the development server:

    python manage.py runserver
    
For stripe integration, set your Stripe API keys in the settings.py file: Go to https://dashboard.stripe.com/apikeys to get your keys.

And update the following lines in car_store/settings.py:

    STRIPE_PUBLIC_KEY = 'your_stripe_public_key'
    STRIPE_SECRET_KEY = 'your_stripe_secret_key'
    
Note:- use python 3.14 or above
