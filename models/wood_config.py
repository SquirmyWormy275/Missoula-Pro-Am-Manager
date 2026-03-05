"""
WoodConfig model for Virtual Woodboss material planning.

Stores per-tournament wood species and size configuration for chopping
blocks and saw logs. One row per (tournament_id, config_key).

config_key conventions:
  Block events:  block_{category}_{type}_{gender}
                 e.g. block_underhand_college_M
                      block_standing_pro_F
                      block_springboard_pro_open
  Saw logs:      log_general  (Single Buck, Double Buck, J&J, Hot Saw)
                 log_stock    (Stock Saw — may differ from general)
                 log_op       (Obstacle Pole — independent species/size)
                 log_cookie   (Cookie Stack — independent species/size)
  Relay blocks:  block_relay_underhand  (Pro-Am Relay underhand butcher block)
                 block_relay_standing   (Pro-Am Relay standing butcher block)
                 These use count_override instead of enrollment-derived counts.
"""
from database import db


class WoodConfig(db.Model):
    """Wood species and size configuration for a single event group within a tournament."""

    __tablename__ = 'wood_configs'

    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournaments.id'), nullable=False)

    # Identifies the event group + division (see module docstring for conventions)
    config_key = db.Column(db.String(100), nullable=False)

    # Wood spec
    species = db.Column(db.Text, nullable=True)
    size_value = db.Column(db.Float, nullable=True)
    size_unit = db.Column(db.String(4), nullable=False, default='in')  # 'in' or 'mm'
    notes = db.Column(db.Text, nullable=True)
    # Manual count override — used for relay blocks and other non-enrollment-derived counts
    count_override = db.Column(db.Integer, nullable=True)

    __table_args__ = (
        db.UniqueConstraint('tournament_id', 'config_key', name='uq_wood_config_tournament_key'),
    )

    def __repr__(self):
        return f'<WoodConfig {self.config_key} t={self.tournament_id} {self.size_value}{self.size_unit} {self.species}>'

    def display_size(self):
        """Return human-readable size string."""
        if self.size_value is None:
            return '—'
        val = int(self.size_value) if self.size_value == int(self.size_value) else self.size_value
        unit = '"' if self.size_unit == 'in' else ' mm'
        return f'{val}{unit}'
