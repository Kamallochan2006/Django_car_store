from pathlib import Path
from django.core.management.base import BaseCommand
from django.core.files import File
from django.conf import settings
from car.models import Car, CarColor, CarImage

class Command(BaseCommand):
    help = 'Import car images from car_images folder based on naming convention [carname][colorname].webp'

    def handle(self, *args, **options):
        # source folder: car_images
        source_dir = Path('car_images')
        
        if not source_dir.exists():
            self.stdout.write(self.style.ERROR(f"Source directory not found: {source_dir}"))
            return

        self.stdout.write(f"Scanning directory: {source_dir}")
        
        # Get all webp files
        image_files = list(source_dir.glob('*.webp'))
        self.stdout.write(f"Found {len(image_files)} .webp files.")

        if not image_files:
            return

        # 1. Load Cars and build slugs (Brand+Name)
        # Sort by slug length DESCENDING to match "Alto K10" before "Alto"
        cars = Car.objects.prefetch_related('colors').all()
        car_map = [] # List of (slug, car_obj)
        for car in cars:
            # Slug: marutisuzukialtok10
            slug = (car.brand + car.name).replace(" ", "").lower()
            car_map.append((slug, car))
        
        # Sort by length desc
        car_map.sort(key=lambda x: len(x[0]), reverse=True)

        # 2. Get all unique colors
        # Sort by length DESCENDING to match "Metallic Silky Silver" before "Silver"
        unique_colors = set(CarColor.objects.values_list('name', flat=True))
        sorted_colors = sorted(unique_colors, key=lambda x: len(x.replace(" ", "")), reverse=True)
        
        matches_found = 0
        images_stats = 0
        
        for img_path in image_files:
            filename = img_path.name # e.g. MarutiSuzukiAltoK10VxiMetallicSilkySilver.webp
            stem = img_path.stem.lower() # marutisuzukialtok10vximetallicsilkysilver
            
            # 3. Match Color Suffix
            matched_color_name = None
            car_prefix = None
            
            for color_name in sorted_colors:
                clean_color = color_name.replace(" ", "").lower()
                if stem.endswith(clean_color):
                    matched_color_name = color_name
                    # Remove color from end to get the prefix (Brand+Name+Variant)
                    # e.g. marutisuzukialtok10vxi
                    car_prefix = stem[:-len(clean_color)]
                    break
            
            if not matched_color_name or not car_prefix:
                # self.stdout.write(self.style.WARNING(f"Skipped {filename}: Could not match color suffix."))
                continue
                
            # 4. Match Car Prefix
            # We look for a car slug that matches the START of car_prefix
            matched_car = None
            
            for slug, car in car_map:
                if car_prefix.startswith(slug):
                    matched_car = car
                    # We match the longest slug first, so this is our best match
                    break
            
            if matched_car:
                # 5. Verify Car has this Color (and get the specific CarColor object)
                target_car_color = None
                for cc in matched_car.colors.all():
                    if cc.name == matched_color_name:
                        target_car_color = cc
                        break
                
                if target_car_color:
                    # MATCH FOUND!
                    
                    # Check if image already exists for this color in DB
                    if CarImage.objects.filter(car_color=target_car_color).exists():
                         self.stdout.write(f"  Skipping {matched_car.name} - {matched_color_name} (Already imported)")
                         continue

                    try:
                        self.stdout.write(self.style.SUCCESS(f"  Mapping {filename} -> {matched_car.brand} {matched_car.name} [{matched_color_name}]"))
                        
                        with img_path.open('rb') as f:
                            car_image = CarImage(
                                car=matched_car,
                                car_color=target_car_color,
                                caption=f"{matched_car.name} {matched_color_name}",
                                is_primary=True,
                                order=0
                            )
                            # Save with original filename
                            car_image.image.save(img_path.name, File(f), save=True)
                            images_stats += 1
                            matches_found += 1
                    except Exception as e:
                        self.stdout.write(self.style.ERROR(f"  Failed to save {filename}: {e}"))
                else:
                    self.stdout.write(self.style.WARNING(f"  Car matched ({matched_car.name}) but color '{matched_color_name}' not available for it."))
            else:
                 pass
                 # self.stdout.write(self.style.WARNING(f"  Color matched ({matched_color_name}) but no car matched prefix '{car_prefix}'"))

        self.stdout.write(self.style.SUCCESS(f"Done! Processed {len(image_files)} files. Successfully imported {images_stats} images."))
