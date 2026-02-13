import csv
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote_plus

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from car.models import Car, CarColor, CarImage


class Command(BaseCommand):
    help = "Replace all car records with rows from a CSV dataset."

    DEFAULT_IMAGE_TEMPLATE = ""

    def add_arguments(self, parser):
        default_path = Path(settings.BASE_DIR) / "car.csv"
        parser.add_argument(
            "--file",
            dest="csv_file",
            default=str(default_path),
            help="Path to the CSV file to import (default: car.csv in project root)",
        )

    def handle(self, *args, **options):
        csv_path = Path(options["csv_file"])
        if not csv_path.is_absolute():
            csv_path = Path(settings.BASE_DIR) / csv_path

        if not csv_path.exists():
            raise CommandError(f"CSV file not found: {csv_path}")

        self.stdout.write(self.style.WARNING("Deleting existing car entries..."))

        with csv_path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                raise CommandError("CSV file is missing headers.")

            # Group rows by unique car (brand + name)
            car_groups = {}  # (brand, name) -> {car_data, colors: [(color, hex_code, image_url)]}
            skipped_rows = 0

            for row in reader:
                car_data = self._convert_row(row)
                if not car_data:
                    skipped_rows += 1
                    continue
                
                
                # Extract color info
                color = (row.get('Color') or row.get('color') or '').strip()
                hex_code = (row.get('Color_hex_code') or row.get('color_hex_code') or row.get('Hex_Code') or row.get('hex_code') or '').strip()
                
                # Create unique key for this car
                car_key = (car_data['brand'], car_data['name'])
                
                if car_key not in car_groups:
                    # First time seeing this car
                    car_groups[car_key] = {
                        'car_data': car_data,
                        'colors': []
                    }
                
                # Add color if we have one
                if color:
                    car_groups[car_key]['colors'].append({
                        'color': color,
                        'hex_code': hex_code
                    })

        if not car_groups:
            raise CommandError("No valid car rows found in the provided CSV.")

        with transaction.atomic():
            CarImage.objects.all().delete()
            CarColor.objects.all().delete()
            Car.objects.all().delete()
            
            # Create unique cars
            cars_to_create = [Car(**group['car_data']) for group in car_groups.values()]
            created_cars = Car.objects.bulk_create(cars_to_create, batch_size=200)
            
            # Map car_key to created car
            car_keys = list(car_groups.keys())
            car_key_to_car = {car_keys[i]: created_cars[i] for i in range(len(created_cars))}
            
            # Create CarColor and CarImage records
            car_colors_to_create = []
            color_map = {}  # (car_id, color_name) -> CarColor
            
            for car_key, group in car_groups.items():
                car = car_key_to_car[car_key]
                for idx, color_info in enumerate(group['colors']):
                    color_name = color_info['color']
                    hex_code = color_info['hex_code']
                    
                    if color_name:
                        color_key = (car.id, color_name)
                        if color_key not in color_map:
                            car_color = CarColor(car=car, name=color_name, hex_code=hex_code, order=idx)
                            car_colors_to_create.append(car_color)
                            color_map[color_key] = car_color
            
            # Bulk create colors
            if car_colors_to_create:
                CarColor.objects.bulk_create(car_colors_to_create, batch_size=200)
                self.stdout.write(self.style.SUCCESS(f"Created {len(car_colors_to_create)} car colors."))
                # Refresh to get PKs
                for car_color in CarColor.objects.all():
                    color_map[(car_color.car_id, car_color.name)] = car_color
            
            # Note: CarImage records are no longer created during import.
            # Images should be uploaded manually via the Admin panel.

        self.stdout.write(self.style.SUCCESS(f"Imported {len(car_groups)} unique cars."))
        if skipped_rows:
            self.stdout.write(self.style.WARNING(f"Skipped {skipped_rows} rows due to missing critical data."))

    def _convert_row(self, row):
        brand = (row.get("Maker") or "").strip()
        model = (row.get("Model") or "").strip()
        variant = (row.get("Variant") or "").strip()
        name = " ".join(part for part in [model, variant] if part).strip()
        if not brand or not name:
            return None

        price = self._parse_price(row.get("Ex-Showroom_Price"))
        if price is None:
            return None

        fuel_type = self._normalize_choice(
            row.get("Fuel_Type"),
            {choice[0] for choice in Car.FUEL_CHOICES},
            fallback="petrol",
        )
        transmission = self._infer_transmission(row)

        mileage = self._compose_mileage(row)
        engine = self._compose_engine(row)
        description = self._compose_description(row, name)
        image_url = self._get_image_url(row, brand, name)
        selling_price = self._parse_price(row.get("Selling_Price"))
        model_year = self._parse_model_year(row, model, variant)
        
        # Skip if no model year found
        if model_year is None:
            return None

        car_data = {
            "name": name,
            "brand": brand,
            "model_year": model_year,
            "price": price,
            "selling_price": selling_price,
            "fuel_type": fuel_type,
            "transmission": transmission,
            "mileage": mileage,
            "engine": engine,
            "description": description,
            "is_available": True,
        }
        return car_data
    
    def _parse_model_year(self, row, model, variant):
        """Extract model year from CSV columns or parse from model/variant name."""
        # Check for explicit year columns
        year_columns = ['Year', 'Model_Year', 'model_year', 'year', 'Year_Made', 
                        'Manufacturing_Year', 'Mfg_Year', 'Manufacture_Year']
        
        for col in year_columns:
            year_str = (row.get(col) or "").strip()
            if year_str:
                try:
                    year = int(re.sub(r'[^0-9]', '', year_str))
                    if 1990 <= year <= 2030:
                        return year
                except ValueError:
                    pass
        
        # Try to extract year from Model or Variant fields
        combined = f"{model} {variant}".strip()
        year_match = re.search(r'\b(19[9][0-9]|20[0-2][0-9])\b', combined)
        if year_match:
            return int(year_match.group(1))
        
        # No year found
        return None

    def _parse_price(self, price_str):
        if not price_str:
            return None
        
        # Clean the string
        digits = re.sub(r"[^0-9.]", "", str(price_str).replace(',', ''))
        
        if not digits:
            return None
            
        try:
            value = Decimal(digits)
            
            # --- THE FIX: Scale the value HERE, before saving to DB ---
            # If value is tiny (e.g., 2.5), it means Crores/Lakhs. Scale it up.
            if value > 0 and value < 500:
                # Assuming 2.5 means 2.5 Crores
                value = value * Decimal('10000000') 
                
        except InvalidOperation:
            return None
            
        return value

    def _normalize_choice(self, raw_value, valid_choices, fallback):
        if not raw_value:
            return fallback
        normalized = raw_value.strip().lower()
        if normalized in valid_choices:
            return normalized
        return fallback

    def _infer_transmission(self, row):
        variant = (row.get("Variant") or "").lower()
        trans_type = (row.get("Type") or "").lower()
        if any(keyword in variant for keyword in ("amt", "automatic")):
            return "automatic"
        if trans_type.startswith("auto"):
            return "automatic"
        return "manual"

    def _compose_mileage(self, row):
        parts = []
        city = row.get("City_Mileage")
        highway = row.get("Highway_Mileage")
        arai = row.get("ARAI_Certified_Mileage")
        if city:
            parts.append(f"City: {city}")
        if highway:
            parts.append(f"Highway: {highway}")
        if arai:
            parts.append(f"ARAI: {arai}")
        return " | ".join(parts) or "Mileage data unavailable"

    def _compose_engine(self, row):
        displacement = row.get("Displacement")
        cylinders = row.get("Cylinders")
        power = row.get("Power")
        torque = row.get("Torque")
        segments = []
        if displacement:
            segments.append(displacement)
        if cylinders:
            segments.append(f"{cylinders} cylinders")
        if power:
            segments.append(power)
        if torque:
            segments.append(torque)
        return " | ".join(segments) or "Engine details unavailable"

    def _compose_description(self, row, name):
        body_type = row.get("Body_Type")
        drivetrain = row.get("Drivetrain")
        fuel_system = row.get("Fuel_System")
        highlights = []
        if body_type:
            highlights.append(body_type)
        if drivetrain:
            highlights.append(drivetrain)
        if fuel_system:
            highlights.append(f"Fuel system: {fuel_system}")
        base_text = ", ".join(highlights) if highlights else "Feature details unavailable"
        return f"{name} - {base_text}. Imported from curated dataset."

    def _build_image_url(self, brand, name):
        label = quote_plus(f"{brand} {name}")
        return self.DEFAULT_IMAGE_TEMPLATE.format(label=label)

    def _get_image_url(self, row, brand, name):
        """Check if CSV has image URL column, otherwise generate placeholder."""
        # Check for various possible column names for image URL
        image_columns = ['Image_URL', 'image_url', 'ImageURL', 'Image', 'image', 
                         'Photo_URL', 'photo_url', 'Picture_URL', 'picture_url',
                         'Img_URL', 'img_url', 'Car_Image', 'car_image']
        
        for col in image_columns:
            image_url = (row.get(col) or "").strip()
            if image_url and (image_url.startswith('http://') or image_url.startswith('https://')):
                return image_url
        
        # No valid image URL found, use placeholder
        return self._build_image_url(brand, name)
