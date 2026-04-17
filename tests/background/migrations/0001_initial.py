from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies: list = []

    operations = [
        migrations.CreateModel(
            name='Widget',
            fields=[
                ('id', models.BigAutoField(
                    auto_created=True, primary_key=True, serialize=False, verbose_name='ID',
                )),
                ('status', models.CharField(default='draft', max_length=32)),
                ('se_log', models.TextField(blank=True, default='')),
                ('cb_log', models.TextField(blank=True, default='')),
                ('kwargs_seen', models.JSONField(blank=True, default=list)),
            ],
            options={'app_label': 'bg_tests'},
        ),
    ]
