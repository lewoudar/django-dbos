from django.db import models


class Signup(models.Model):
    email = models.EmailField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.email
