"""
═══════════════════════════════════════════════════════════════════════════════
TMS & BUSINESS INTELLIGENCE VRAC — v2.0
LH Côte d'Ivoire | Mapping Vrac & Planification Flotte
═══════════════════════════════════════════════════════════════════════════════
Corrections v2.0 :
  - Fix colonne 'statut' (vs 'status') pour FLOTTE_LH
  - Fix params chargés avant usage dans blocs except
  - Onglet FLOTTE_LH intégré dans l'analyse
  - Dispatch : vérification surplus réel avant affectation
  - Alerte rotations depuis PARAMETRES (plus hardcodé)
  - Carte : filtre par zone + popup enrichi avec silos
  - Ajout onglet STOCKAGE_SILOS dans l'analyse
  - Création automatique onglet LIVRAISONS_JOUR si absent
  - Authentification mutualisée (pas de double connexion)
═══════════════════════════════════════════════════════════════════════════════
"""

import math
import datetime
import os

import folium
import gspread
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from google.oauth2.service_account import Credentials
from streamlit_folium import st_folium

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION PAGE
# ══════════════════════════════════════════════════════════════════════════════
st.set_page_config(
    page_title="LH CI — TMS Vrac",
    page_icon="🚛",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── IDs & Chemins ─────────────────────────────────────────────────────────────
SHEET_ID             = "14ix_pet8yn7b_yG7rfd7GwqcsNFEU4BSCVzWr8FVRBc"
BASE_DIR             = os.path.dirname(os.path.abspath(__file__))
SERVICE_ACCOUNT_FILE = os.path.join(BASE_DIR, "service_account.json")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Noms des onglets Google Sheets
TAB_SITES     = "CLIENTS_SITES"
TAB_SILOS     = "STOCKAGE_SILOS"
TAB_FLOTTE_C  = "FLOTTE_CLIENTS"
TAB_FLOTTE_LH = "FLOTTE_LH"
TAB_VOLUMES   = "VOLUMES_SAP"
TAB_PARAMS    = "PARAMETRES"
TAB_LIVR      = "LIVRAISONS_JOUR"

# ══════════════════════════════════════════════════════════════════════════════
# UTILITAIRES
# ══════════════════════════════════════════════════════════════════════════════

def get_credentials() -> Credentials:
    """Retourne les credentials Google — Cloud Secrets ou fichier local."""
    if "gcp_service_account" in st.secrets:
        creds_dict = dict(st.secrets["gcp_service_account"])
        # Correction du retour à la ligne dans la clé privée (bug courant Streamlit Cloud)
        if "private_key" in creds_dict:
            creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
        return Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    elif os.path.exists(SERVICE_ACCOUNT_FILE):
        return Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    else:
        st.error(
            "❌ Authentification impossible : ni `service_account.json` local "
            "ni Secrets Streamlit Cloud détectés."
        )
        st.stop()


def open_workbook() -> gspread.Spreadsheet:
    """Ouvre le Google Sheets. Utilisé en dehors du cache (pour les écritures)."""
    creds = get_credentials()
    gc    = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID)


def fetch_sheet(wb: gspread.Spreadsheet, tab_name: str) -> pd.DataFrame:
    """
    Lit un onglet et détecte automatiquement la ligne d'en-tête technique
    (celle qui contient client_id / parametre / camion_id / id_commande).
    Retourne un DataFrame vide si l'onglet est absent ou illisible.
    """
    try:
        ws   = wb.worksheet(tab_name)
        rows = ws.get_all_values()
    except gspread.WorksheetNotFound:
        return pd.DataFrame()
    except Exception as exc:
        st.warning(f"⚠ Onglet '{tab_name}' illisible : {exc}")
        return pd.DataFrame()

    if not rows:
        return pd.DataFrame()

    HEADER_MARKERS = {"client_id", "parametre", "camion_id", "id_commande", "silo_id"}
    for i, row in enumerate(rows):
        cleaned = [str(c).strip() for c in row]
        if HEADER_MARKERS & set(cleaned):
            return pd.DataFrame(rows[i + 1:], columns=cleaned)

    return pd.DataFrame()


def to_numeric_safe(series: pd.Series) -> pd.Series:
    """Convertit en numérique en gérant la virgule française et les espaces."""
    # On force la conversion en texte pour nettoyer proprement
    s_cleaned = series.astype(str).str.replace(',', '.').str.replace(' ', '')
    return pd.to_numeric(s_cleaned, errors="coerce").fillna(0.0)


def param_val(df_params: pd.DataFrame, key: str, default):
    """Extrait une valeur du DataFrame PARAMETRES de façon sécurisée."""
    try:
        return type(default)(df_params.loc[key, "valeur"])
    except Exception:
        return default


# ══════════════════════════════════════════════════════════════════════════════
# CHARGEMENT DES DONNÉES (mis en cache)
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=600, show_spinner="Chargement des données depuis Google Sheets…")
def load_data():
    """
    Charge tous les onglets nécessaires, effectue les jointures et calculs,
    et retourne un dict de DataFrames + les paramètres globaux.
    """
    creds = get_credentials()
    gc    = gspread.authorize(creds)
    try:
        wb = gc.open_by_key(SHEET_ID)
    except Exception as e:
        st.error(f"❌ Connexion au classeur Google Sheets impossible : {e}")
        st.stop()

    # ── Lecture des onglets ───────────────────────────────────────────────────
    df_params_raw = fetch_sheet(wb, TAB_PARAMS)
    df_sites      = fetch_sheet(wb, TAB_SITES)
    df_silos      = fetch_sheet(wb, TAB_SILOS)
    df_flotte_c   = fetch_sheet(wb, TAB_FLOTTE_C)
    df_flotte_lh  = fetch_sheet(wb, TAB_FLOTTE_LH)
    df_volumes    = fetch_sheet(wb, TAB_VOLUMES)

    # ── Paramètres globaux ────────────────────────────────────────────────────
    params = pd.DataFrame()
    if not df_params_raw.empty and "parametre" in df_params_raw.columns:
        params = df_params_raw.set_index("parametre")

    spot_ref          = param_val(params, "tarif_spot_ref_fcfa_t",   4500.0)
    tonnage_std       = param_val(params, "tonnage_camion_std",          27.0)
    coef_remplissage  = param_val(params, "coef_remplissage_min",         0.92)
    usine_lat         = param_val(params, "usine_latitude",              5.3599)
    usine_lon         = param_val(params, "usine_longitude",            -4.0083)
    seuil_rotations   = param_val(params, "heures_dispo_camion_j",         40)   # rotations/jour alerte
    nb_mois_hist      = param_val(params, "nb_mois_historique",             3)

    # ── Gardes-fous données vitales ───────────────────────────────────────────
    if df_sites.empty or "client_id" not in df_sites.columns:
        st.error("❌ Onglet CLIENTS_SITES introuvable ou mal formaté.")
        st.stop()

    # ── Nettoyage des client_id ───────────────────────────────────────────────
    for df_tmp in [df_sites, df_flotte_c, df_volumes, df_flotte_lh, df_silos]:
        if not df_tmp.empty and "client_id" in df_tmp.columns:
            df_tmp["client_id"] = df_tmp["client_id"].astype(str).str.strip()

    # ── Conversions numériques ────────────────────────────────────────────────
    for col in ["latitude", "longitude", "distance_usine_km", "temps_trajet_min"]:
        if col in df_sites.columns:
            df_sites[col] = to_numeric_safe(df_sites[col])

    # Capacité mensuelle flotte clients
    # Le nom de la colonne peut varier selon la version du Sheet
    cap_col = next((c for c in ["cap_mensuelle_t", "capacite_mensuelle_t"]
                    if c in df_flotte_c.columns), None)
    if cap_col and not df_flotte_c.empty:
        df_flotte_c[cap_col] = to_numeric_safe(df_flotte_c[cap_col])

    # Volume EXW dans SAP
    vol_col = "volume_exw_t" if "volume_exw_t" in df_volumes.columns else None
    if vol_col and not df_volumes.empty:
        df_volumes[vol_col] = to_numeric_safe(df_volumes[vol_col])

    # Colonnes numériques FLOTTE_LH
    for col in ["capacite_t", "cap_mensuelle_t", "cout_voyage_fcfa", "cout_tonne_fcfa",
                "jours_actifs_semaine", "rotations_cibles_j"]:
        if not df_flotte_lh.empty and col in df_flotte_lh.columns:
            df_flotte_lh[col] = to_numeric_safe(df_flotte_lh[col])

    # Capacité silos
    for col in ["capacite_utile_t", "capacite_nominale_t", "taux_rotation_jours"]:
        if not df_silos.empty and col in df_silos.columns:
            df_silos[col] = to_numeric_safe(df_silos[col])

    # ── Agrégations ───────────────────────────────────────────────────────────
    agg_flotte = pd.DataFrame()
    if not df_flotte_c.empty and cap_col:
        agg_flotte = (
            df_flotte_c.groupby("client_id")
            .agg(capacite_t_mois=(cap_col, "sum"))
            .reset_index()
        )

    agg_volumes = pd.DataFrame()
    if not df_volumes.empty and vol_col:
        agg_volumes = (
            df_volumes.groupby("client_id")
            .agg(besoin_exw_t_mois=(vol_col, "mean"))
            .reset_index()
        )

    agg_silos = pd.DataFrame()
    if not df_silos.empty and "capacite_utile_t" in df_silos.columns:
        agg_silos = (
            df_silos.groupby("client_id")
            .agg(
                capacite_silo_totale_t=("capacite_utile_t", "sum"),
                nb_silos=("silo_id", "count"),
            )
            .reset_index()
        )

    # ── Jointure principale ───────────────────────────────────────────────────
    df_main = df_sites.copy()
    if not agg_flotte.empty:
        df_main = df_main.merge(agg_flotte, on="client_id", how="left")
    # Garantir la colonne même si la jointure n'a rien apporté
    if "capacite_t_mois" not in df_main.columns:
        df_main["capacite_t_mois"] = 0.0

    if not agg_volumes.empty:
        df_main = df_main.merge(agg_volumes, on="client_id", how="left")
    if "besoin_exw_t_mois" not in df_main.columns:
        df_main["besoin_exw_t_mois"] = 0.0

    if not agg_silos.empty:
        df_main = df_main.merge(agg_silos, on="client_id", how="left")
    if "capacite_silo_totale_t" not in df_main.columns:
        df_main["capacite_silo_totale_t"] = 0.0
    if "nb_silos" not in df_main.columns:
        df_main["nb_silos"] = 0

    # fillna sur les colonnes numériques issues des jointures (left join → NaN possibles)
    for col, default in [
        ("capacite_t_mois", 0.0),
        ("besoin_exw_t_mois", 0.0),
        ("capacite_silo_totale_t", 0.0),
        ("nb_silos", 0),
    ]:
        df_main[col] = to_numeric_safe(df_main[col])

    # ── Calculs surplus & finance — toujours créés, même si données vides ────
    df_main["surplus_t_mois"]         = (df_main["capacite_t_mois"] - df_main["besoin_exw_t_mois"]).clip(lower=0)
    df_main["tarif_spot_ref"]         = spot_ref
    df_main["tarif_cible_client"]     = spot_ref * 0.82          # −18% vs spot
    df_main["gain_par_tonne"]         = spot_ref - df_main["tarif_cible_client"]
    df_main["economie_pot_mensuelle"] = df_main["surplus_t_mois"] * df_main["gain_par_tonne"]

    return {
        "main":       df_main,
        "silos":      df_silos,
        "flotte_c":   df_flotte_c,
        "flotte_lh":  df_flotte_lh,
        "volumes":    df_volumes,
        "params": {
            "spot_ref":         spot_ref,
            "tonnage_std":      tonnage_std,
            "coef_remplissage": coef_remplissage,
            "usine_lat":        usine_lat,
            "usine_lon":        usine_lon,
            "seuil_rotations":  seuil_rotations,
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
# CRÉATION AUTOMATIQUE DE L'ONGLET LIVRAISONS_JOUR (si absent)
# ══════════════════════════════════════════════════════════════════════════════

def ensure_livraisons_tab(wb: gspread.Spreadsheet) -> gspread.Worksheet:
    """Crée l'onglet LIVRAISONS_JOUR avec en-têtes s'il n'existe pas."""
    try:
        return wb.worksheet(TAB_LIVR)
    except gspread.WorksheetNotFound:
        ws = wb.add_worksheet(title=TAB_LIVR, rows=200, cols=12)
        # Titre
        ws.update("A1", [["📅 LH CI — PLANNING LIVRAISONS JOUR"]])
        # En-têtes techniques
        headers = [
            "id_commande", "date_livraison", "client_id", "client_nom",
            "site_livraison", "zone", "volume_t", "nb_voyages",
            "transporteur_assigne", "type_transport",
            "economie_fcfa", "statut",
        ]
        ws.update("A2", [headers])
        return ws


# ══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════════════════════════════════════

with st.sidebar:
    st.image("https://upload.wikimedia.org/wikipedia/commons/thumb/4/4e/LafargeHolcim_logo.svg/320px-LafargeHolcim_logo.svg.png",
             width=160)
    st.markdown("## 🚛 TMS Vrac LH CI")
    st.markdown("---")

    if st.button("🔄 Rafraîchir les données", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.markdown("---")
    st.caption("v2.0 — LH Côte d'Ivoire")
    st.caption(f"Mis à jour : {datetime.date.today().strftime('%d/%m/%Y')}")


# ══════════════════════════════════════════════════════════════════════════════
# CHARGEMENT
# ══════════════════════════════════════════════════════════════════════════════

data       = load_data()
df         = data["main"]
df_silos   = data["silos"]
df_flotte_c  = data["flotte_c"]
df_flotte_lh = data["flotte_lh"]
p          = data["params"]

spot_ref        = p["spot_ref"]
tonnage_std     = p["tonnage_std"]
coef_remplissage= p["coef_remplissage"]
usine_lat       = p["usine_lat"]
usine_lon       = p["usine_lon"]
seuil_rotations = p["seuil_rotations"]

# ══════════════════════════════════════════════════════════════════════════════
# TITRE & KPIs GLOBAUX
# ══════════════════════════════════════════════════════════════════════════════

st.title("🚛 LH Côte d'Ivoire — Pilotage Logistique & Financier Vrac")
st.markdown("---")

c1, c2, c3, c4, c5 = st.columns(5)

surplus_total  = df["surplus_t_mois"].sum()         if "surplus_t_mois"         in df.columns else 0.0
economie_total = df["economie_pot_mensuelle"].sum() if "economie_pot_mensuelle" in df.columns else 0.0
nb_clients_gps = df[(df["latitude"] != 0) & (df["longitude"] != 0)].shape[0] if "latitude" in df.columns else 0

# Flotte LH : correction du nom de colonne statut (pas 'status')
nb_actifs_lh = 0
if not df_flotte_lh.empty:
    col_statut = next((c for c in ["statut", "status", "Statut", "Status"]
                       if c in df_flotte_lh.columns), None)
    if col_statut:
        nb_actifs_lh = (df_flotte_lh[col_statut].str.upper() == "ACTIF").sum()

cap_lh_totale = 0
if not df_flotte_lh.empty and "cap_mensuelle_t" in df_flotte_lh.columns:
    cap_lh_totale = df_flotte_lh["cap_mensuelle_t"].sum()

c1.metric("🏗 Clients cartographiés",
          f"{nb_clients_gps} / {len(df)}",
          delta="GPS OK" if nb_clients_gps == len(df) else f"{len(df)-nb_clients_gps} sans GPS")
c2.metric("📦 Surplus flotte clients",
          f"{surplus_total:,.0f} T/mois",
          delta=f"≈ {surplus_total/tonnage_std:.0f} voyages")
c3.metric("💰 Économie potentielle",
          f"{economie_total/1_000_000:.1f} M FCFA/mois",
          delta=f"{economie_total*12/1_000_000:.1f} M FCFA/an")
c4.metric("🚛 Flotte LH active",
          f"{nb_actifs_lh} camions",
          delta=f"{cap_lh_totale:,.0f} T/mois")
c5.metric("📊 Tarif spot réf.",
          f"{spot_ref:,.0f} FCFA/T",
          delta=f"Cible contrat : {spot_ref*0.82:,.0f} FCFA/T",
          delta_color="inverse")

st.markdown("---")

# ══════════════════════════════════════════════════════════════════════════════
# ONGLETS PRINCIPAUX
# ══════════════════════════════════════════════════════════════════════════════

tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "🗺️ Carte Interactive",
    "📦 Surplus & Silos",
    "🚛 Flotte LH",
    "💸 Opportunités Financières",
    "📅 Dispatch & Planification",
])

# ─────────────────────────────────────────────────────────────────────────────
# ONGLET 1 — CARTE INTERACTIVE
# ─────────────────────────────────────────────────────────────────────────────
with tab1:
    st.subheader("Cartographie des sites clients")

    col_filtre1, col_filtre2, col_filtre3 = st.columns(3)
    with col_filtre1:
        zones_dispo = ["Toutes"] + sorted(df["zone"].dropna().unique().tolist())
        zone_sel    = st.selectbox("Filtrer par zone", zones_dispo)
    with col_filtre2:
        afficher_surplus = st.checkbox("Afficher seulement les clients avec surplus", value=False)
    with col_filtre3:
        rayon_cercle = st.slider("Rayon cercle surplus (m)", 100, 2000, 500, step=100)

    df_map = df[(df["latitude"] != 0) & (df["longitude"] != 0)].copy()

    if zone_sel != "Toutes":
        df_map = df_map[df_map["zone"] == zone_sel]
    if afficher_surplus:
        df_map = df_map[df_map["surplus_t_mois"] > 0]

    # Agrégation silos par client pour le popup
    silos_par_client = {}
    if not df_silos.empty and "capacite_utile_t" in df_silos.columns:
        silos_par_client = (
            df_silos.groupby("client_id")["capacite_utile_t"]
            .sum().to_dict()
        )

    m = folium.Map(location=[usine_lat, usine_lon], zoom_start=11,
                   tiles="CartoDB positron")

    # Usine
    folium.Marker(
        [usine_lat, usine_lon],
        popup="<b>🏭 Usine LH CI</b>",
        tooltip="Usine LafargeHolcim CI",
        icon=folium.Icon(color="black", icon="industry", prefix="fa"),
    ).add_to(m)

    # Cercles de zones
    for rayon, couleur, label in [
        (40_000, "#003DA5", "Zone A — 40 km"),
        (80_000, "#1F7A4D", "Zone B — 80 km"),
    ]:
        folium.Circle(
            location=[usine_lat, usine_lon],
            radius=rayon,
            color=couleur,
            fill=False,
            weight=1.5,
            dash_array="6",
            tooltip=label,
        ).add_to(m)

    # Markers clients
    for _, row in df_map.iterrows():
        surplus   = row.get("surplus_t_mois", 0)
        cap_silo  = silos_par_client.get(str(row["client_id"]), 0)
        dist_val  = row.get("distance_usine_km", "?")
        tps_val   = row.get("temps_trajet_min", "?")
        dist      = f"{dist_val:.1f}" if isinstance(dist_val, (int, float)) and dist_val != 0 else "?"
        tps       = f"{tps_val:.0f}"  if isinstance(tps_val,  (int, float)) and tps_val  != 0 else "?"
        economie  = row.get("economie_pot_mensuelle", 0)

        if surplus > 200:
            color = "green"
        elif surplus > 50:
            color = "orange"
        elif surplus > 0:
            color = "lightgreen"
        else:
            color = "blue"

        popup_html = f"""
        <div style='font-family:Arial;font-size:12px;min-width:220px'>
          <b style='font-size:13px'>{row['client_nom']}</b><br>
          <hr style='margin:4px 0'>
          🗺 Zone : <b>{row.get('zone','—')}</b> | 
          📍 {dist} km | ⏱ {tps} min<br>
          📦 Surplus flotte : <b>{surplus:,.0f} T/mois</b><br>
          🏗 Capacité silos : <b>{cap_silo:,.0f} T</b><br>
          💰 Économie potentielle : <b>{economie:,.0f} FCFA/mois</b>
        </div>"""

        folium.Marker(
            [float(row["latitude"]), float(row["longitude"])],
            popup=folium.Popup(popup_html, max_width=280),
            tooltip=row["client_nom"],
            icon=folium.Icon(color=color, icon="truck", prefix="fa"),
        ).add_to(m)

        # Cercle proportionnel au surplus
        if surplus > 0:
            folium.Circle(
                location=[float(row["latitude"]), float(row["longitude"])],
                radius=min(surplus * rayon_cercle / 200, 3000),
                color=color,
                fill=True,
                fill_opacity=0.15,
            ).add_to(m)

    st_folium(m, width="100%", height=500)

    # Légende
    col_leg1, col_leg2, col_leg3, col_leg4 = st.columns(4)
    col_leg1.markdown("🟢 **Surplus > 200 T/mois** (Fort potentiel)")
    col_leg2.markdown("🟠 **Surplus 50-200 T/mois** (Moyen)")
    col_leg3.markdown("🔵 **Pas de surplus** (EXW pur)")
    col_leg4.markdown(f"📍 {len(df_map)} sites affichés")


# ─────────────────────────────────────────────────────────────────────────────
# ONGLET 2 — SURPLUS & SILOS
# ─────────────────────────────────────────────────────────────────────────────
with tab2:
    col_s1, col_s2 = st.columns(2)

    with col_s1:
        st.subheader("Top clients — Surplus flotte")
        df_top = (df[df["surplus_t_mois"] > 0]
                  .sort_values("surplus_t_mois", ascending=False)
                  .head(10))
        if not df_top.empty:
            fig_bar = px.bar(
                df_top, x="surplus_t_mois", y="client_nom",
                orientation="h",
                color="zone",
                color_discrete_map={"Zone A": "#003DA5", "Zone B": "#1F7A4D", "Zone C": "#C55A11"},
                labels={"surplus_t_mois": "Surplus (T/mois)", "client_nom": ""},
                text_auto=True,
            )
            fig_bar.update_layout(height=380, margin=dict(l=0, r=0, t=30, b=0))
            st.plotly_chart(fig_bar, use_container_width=True)
        else:
            st.info("Aucun surplus détecté — données flotte clients à compléter.")

    with col_s2:
        st.subheader("Répartition surplus par zone")
        surplus_zone = (df.groupby("zone")["surplus_t_mois"]
                        .sum().reset_index()
                        .rename(columns={"surplus_t_mois": "Surplus (T/mois)"}))
        if not surplus_zone.empty and surplus_zone["Surplus (T/mois)"].sum() > 0:
            fig_pie = px.pie(
                surplus_zone, values="Surplus (T/mois)", names="zone",
                color="zone",
                color_discrete_map={"Zone A": "#003DA5", "Zone B": "#1F7A4D", "Zone C": "#C55A11"},
            )
            fig_pie.update_layout(height=380)
            st.plotly_chart(fig_pie, use_container_width=True)

    st.markdown("---")
    st.subheader("Détail par client")
    cols_display = [c for c in [
        "client_id", "client_nom", "zone", "commune",
        "capacite_t_mois", "besoin_exw_t_mois", "surplus_t_mois",
        "capacite_silo_totale_t", "nb_silos", "economie_pot_mensuelle",
    ] if c in df.columns]
    st.dataframe(
        df[cols_display].sort_values("surplus_t_mois", ascending=False)
        .rename(columns={
            "client_id": "Code SAP",
            "client_nom": "Client",
            "zone": "Zone",
            "commune": "Commune",
            "capacite_t_mois": "Cap. Flotte (T/mois)",
            "besoin_exw_t_mois": "Besoin EXW (T/mois)",
            "surplus_t_mois": "Surplus (T/mois)",
            "capacite_silo_totale_t": "Cap. Silos (T)",
            "nb_silos": "Nb Silos",
            "economie_pot_mensuelle": "Économie (FCFA/mois)",
        }),
        use_container_width=True,
        height=320,
    )


# ─────────────────────────────────────────────────────────────────────────────
# ONGLET 3 — FLOTTE LH
# ─────────────────────────────────────────────────────────────────────────────
with tab3:
    st.subheader("Notre flotte — Propre LH, Sous-traitants & Contractualisés")

    if df_flotte_lh.empty:
        st.warning("Onglet FLOTTE_LH vide ou non trouvé. Compléter le Google Sheets.")
    else:
        # KPIs flotte LH
        col_statut_lh = next((c for c in ["statut", "status"] if c in df_flotte_lh.columns), None)

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Total camions", len(df_flotte_lh))

        if "type_appartenance" in df_flotte_lh.columns:
            nb_propre = (df_flotte_lh["type_appartenance"] == "Propre LH").sum()
            nb_st     = (df_flotte_lh["type_appartenance"] == "Sous-traitant").sum()
            nb_contrat= (df_flotte_lh["type_appartenance"] == "Contractualisé Client").sum()
            k2.metric("Propre LH",              nb_propre)
            k3.metric("Sous-traitants",          nb_st)
            k4.metric("Contractualisés clients", nb_contrat)

        # Graphique répartition par type & zone
        col_g1, col_g2 = st.columns(2)

        with col_g1:
            if "type_appartenance" in df_flotte_lh.columns:
                fig_type = px.pie(
                    df_flotte_lh["type_appartenance"].value_counts().reset_index(),
                    values="count", names="type_appartenance",
                    title="Répartition par type d'appartenance",
                    color_discrete_sequence=["#001F5B", "#C55A11", "#1F7A4D"],
                )
                st.plotly_chart(fig_type, use_container_width=True)

        with col_g2:
            if "zone_affectation" in df_flotte_lh.columns and "cap_mensuelle_t" in df_flotte_lh.columns:
                cap_par_zone = (df_flotte_lh.groupby("zone_affectation")["cap_mensuelle_t"]
                                .sum().reset_index())
                fig_zone = px.bar(
                    cap_par_zone, x="zone_affectation", y="cap_mensuelle_t",
                    title="Capacité mensuelle par zone (T/mois)",
                    color="zone_affectation",
                    color_discrete_map={"Zone A": "#003DA5", "Zone B": "#1F7A4D",
                                        "Zone C": "#C55A11", "Multi-zones": "#7030A0"},
                    labels={"cap_mensuelle_t": "T/mois", "zone_affectation": "Zone"},
                    text_auto=True,
                )
                st.plotly_chart(fig_zone, use_container_width=True)

        # Tableau détaillé
        cols_lh = [c for c in [
            "camion_id", "type_appartenance", "proprietaire_nom", "zone_affectation",
            "capacite_t", "jours_actifs_semaine", "rotations_cibles_j", "cap_mensuelle_t",
            "etat_general", "gps_equipe", "cout_tonne_fcfa", "statut",
        ] if c in df_flotte_lh.columns]
        st.dataframe(df_flotte_lh[cols_lh], use_container_width=True, height=280)


# ─────────────────────────────────────────────────────────────────────────────
# ONGLET 4 — OPPORTUNITÉS FINANCIÈRES
# ─────────────────────────────────────────────────────────────────────────────
with tab4:
    st.subheader("Matrice des opportunités — Contractualisation flotte clients")

    col_f1, col_f2 = st.columns(2)

    with col_f1:
        df_fin = (df[df["economie_pot_mensuelle"] > 0]
                  .sort_values("economie_pot_mensuelle", ascending=False)
                  .head(10))
        if not df_fin.empty:
            fig_fin = px.bar(
                df_fin, x="economie_pot_mensuelle", y="client_nom",
                orientation="h",
                title="Top 10 — Économie mensuelle potentielle (FCFA)",
                color="zone",
                color_discrete_map={"Zone A": "#003DA5", "Zone B": "#1F7A4D", "Zone C": "#C55A11"},
                text_auto=True,
                labels={"economie_pot_mensuelle": "FCFA/mois", "client_nom": ""},
            )
            fig_fin.update_layout(height=380)
            st.plotly_chart(fig_fin, use_container_width=True)

    with col_f2:
        # Waterfall spot vs contractualisé
        categories = ["Coût spot actuel", "Tarif contractualisé\nclient (−18%)", "Économie"]
        valeurs    = [spot_ref, -(spot_ref * 0.18), -(spot_ref * 0.18)]
        couleurs   = ["#C00000", "#1F7A4D", "#1F7A4D"]

        fig_wf = go.Figure(go.Waterfall(
            name="FCFA/T",
            orientation="v",
            measure=["absolute", "relative", "total"],
            x=categories,
            y=[spot_ref, -(spot_ref * 0.18), 0],
            text=[f"{spot_ref:,.0f}", f"−{spot_ref*0.18:,.0f}", f"{spot_ref*0.18:,.0f}"],
            textposition="outside",
            connector={"line": {"color": "rgb(63, 63, 63)"}},
            decreasing={"marker": {"color": "#1F7A4D"}},
            increasing={"marker": {"color": "#C00000"}},
            totals={"marker": {"color": "#003DA5"}},
        ))
        fig_wf.update_layout(
            title="Impact tarifaire (FCFA/T)",
            height=380,
            showlegend=False,
        )
        st.plotly_chart(fig_wf, use_container_width=True)

    st.markdown("---")
    cols_fin = [c for c in [
        "client_id", "client_nom", "zone", "surplus_t_mois",
        "tarif_spot_ref", "tarif_cible_client", "gain_par_tonne", "economie_pot_mensuelle",
    ] if c in df.columns]
    df_fin_table = df[cols_fin].sort_values("economie_pot_mensuelle", ascending=False)
    df_fin_table.columns = [
        "Code SAP", "Client", "Zone", "Surplus (T/mois)",
        "Tarif Spot (FCFA/T)", "Tarif Cible (FCFA/T)",
        "Gain/T (FCFA)", "Économie Mensuelle (FCFA)",
    ][:len(cols_fin)]
    st.dataframe(df_fin_table, use_container_width=True)


# ─────────────────────────────────────────────────────────────────────────────
# ONGLET 5 — DISPATCH & PLANIFICATION
# ─────────────────────────────────────────────────────────────────────────────
with tab5:
    st.subheader("📅 Planification Multi-jours — Ordres de Transport")

    # Construction liste destinations à partir des clients avec GPS
    df_avec_gps = df[df["latitude"].apply(lambda x: isinstance(x, (int, float)) and x != 0)].copy()
    col_site = "site_nom" if "site_nom" in df_avec_gps.columns else "commune"

    df_avec_gps["destination_complete"] = (
        df_avec_gps["client_nom"].astype(str)
        + " — "
        + df_avec_gps.get(col_site, df_avec_gps.get("commune", "")).astype(str)
        + " ("
        + df_avec_gps["zone"].astype(str)
        + ")"
    )
    liste_destinations = sorted(df_avec_gps["destination_complete"].dropna().unique().tolist())

    if not liste_destinations:
        st.warning("⚠ Aucun client avec coordonnées GPS. Compléter CLIENTS_SITES via le script de géocodage.")
    else:
        st.info("💡 Cliquez **+** pour ajouter un ordre. Sélectionnez le couple Client — Site dans la liste. "
                "Le système affecte automatiquement le transporteur optimal et calcule l'économie.")

        if "df_commandes" not in st.session_state:
            st.session_state.df_commandes = pd.DataFrame([{
                "date_livraison":   datetime.date.today(),
                "destination":      liste_destinations[0],
                "volume_t":         tonnage_std,
            }])

        commandes = st.data_editor(
            st.session_state.df_commandes,
            num_rows="dynamic",
            column_config={
                "date_livraison": st.column_config.DateColumn(
                    "Date de livraison", required=True, format="DD/MM/YYYY"),
                "destination": st.column_config.SelectboxColumn(
                    "Destination (Client — Site — Zone)",
                    options=liste_destinations, required=True),
                "volume_t": st.column_config.NumberColumn(
                    "Volume (T)", min_value=0, step=tonnage_std,
                    help=f"Tonnage camion std = {tonnage_std} T"),
            },
            use_container_width=True,
            key="editeur_dispatch",
        )

        if st.button("⚡ Générer le plan de transport", type="primary"):
            df_cmd = pd.DataFrame(commandes)
            df_cmd = df_cmd[df_cmd["destination"].astype(str).str.strip() != ""]

            if df_cmd.empty:
                st.warning("Aucune commande valide.")
            else:
                # Calcul du surplus encore disponible (évite double-affectation)
                surplus_restant = df_avec_gps.set_index("destination_complete")["surplus_t_mois"].to_dict()

                plan       = []
                sheets_rows= []
                seq        = 1

                for _, row_cmd in df_cmd.iterrows():
                    dest    = str(row_cmd["destination"])
                    vol     = float(row_cmd["volume_t"])
                    date_obj= row_cmd["date_livraison"]

                    if isinstance(date_obj, str):
                        date_str = date_obj
                    else:
                        date_str = date_obj.strftime("%Y-%m-%d")

                    code_cmd    = f"CMD-{date_str.replace('-','')}-{seq:03d}"
                    nb_voyages  = math.ceil(vol / (tonnage_std * coef_remplissage))
                    seq        += 1

                    match = df_avec_gps[df_avec_gps["destination_complete"] == dest]
                    if not match.empty:
                        client_nom  = match.iloc[0]["client_nom"]
                        client_id   = match.iloc[0]["client_id"]
                        zone_dest   = match.iloc[0]["zone"]
                        site_dest   = match.iloc[0].get(col_site, "—")
                    else:
                        client_nom  = dest
                        client_id   = "—"
                        zone_dest   = "Zone A"
                        site_dest   = "—"

                    # Choix du transporteur :
                    # 1) Client de la même zone avec surplus disponible
                    # 2) Flotte LH contractualisée
                    # 3) Sous-traitant spot
                    partenaires = df_avec_gps[
                        (df_avec_gps["zone"] == zone_dest) &
                        (df_avec_gps["surplus_t_mois"] > 0) &
                        (df_avec_gps["client_id"] != client_id)
                    ].sort_values("surplus_t_mois", ascending=False)

                    transporteur_type = ""
                    transporteur_nom  = ""
                    gain              = 0

                    for _, part in partenaires.iterrows():
                        key_surp = part["destination_complete"]
                        if surplus_restant.get(key_surp, 0) >= vol:
                            surplus_restant[key_surp] -= vol
                            transporteur_nom  = part["client_nom"]
                            transporteur_type = "🤝 Flotte Client"
                            gain = nb_voyages * tonnage_std * (spot_ref * 0.18)
                            break

                    if not transporteur_nom:
                        # Fallback : flotte LH propre ou contractualisée
                        if not df_flotte_lh.empty and "zone_affectation" in df_flotte_lh.columns:
                            lh_zone = df_flotte_lh[
                                df_flotte_lh["zone_affectation"].isin([zone_dest, "Multi-zones"])
                            ]
                            if not lh_zone.empty:
                                transporteur_nom  = "Flotte LH"
                                transporteur_type = "🚛 Flotte LH"
                                gain = nb_voyages * tonnage_std * (spot_ref * 0.10)

                    if not transporteur_nom:
                        transporteur_nom  = "Sous-traitant Spot"
                        transporteur_type = "🔴 Spot"
                        gain = 0

                    plan.append({
                        "Date":           date_str,
                        "Code":           code_cmd,
                        "Client":         client_nom,
                        "Site":           site_dest,
                        "Zone":           zone_dest,
                        "Volume (T)":     f"{vol:.0f}",
                        "Voyages":        nb_voyages,
                        "Transporteur":   f"{transporteur_type} — {transporteur_nom}",
                        "Économie (FCFA)":f"{gain:,.0f}",
                    })
                    sheets_rows.append([
                        code_cmd, date_str, client_id, client_nom,
                        site_dest, zone_dest, vol, nb_voyages,
                        transporteur_nom, transporteur_type.replace("🤝 ", "").replace("🚛 ", "").replace("🔴 ", "") if transporteur_type else "Spot",
                        gain, "Planifié",
                    ])

                # Affichage du plan
                st.markdown("### 📋 Plan de transport généré")
                df_plan = pd.DataFrame(plan)
                st.dataframe(df_plan, use_container_width=True)

                # Alertes capacité par jour
                st.markdown("### ⚠ Alertes capacité journalière")
                voyages_par_jour = df_plan.groupby("Date")["Voyages"].sum()
                for jour, total in voyages_par_jour.items():
                    if total > seuil_rotations:
                        st.error(f"🚨 {jour} : {total} rotations planifiées > seuil ({seuil_rotations}). Risque congestion rampe.")
                    else:
                        st.success(f"✅ {jour} : {total} rotations — capacité OK.")

                # Sauvegarde Google Sheets
                st.markdown("---")
                with st.spinner("💾 Enregistrement dans Google Sheets…"):
                    try:
                        wb      = open_workbook()
                        ws_livr = ensure_livraisons_tab(wb)

                        ws_livr.append_rows(
                            sheets_rows,
                            value_input_option="USER_ENTERED",
                            table_range="A3",
                        )
                        st.success(f"✅ {len(sheets_rows)} ordres enregistrés dans l'onglet '{TAB_LIVR}'.")
                        # Réinitialise le formulaire pour éviter les doublons
                        if "df_commandes" in st.session_state:
                            del st.session_state["df_commandes"]
                        import time as _time; _time.sleep(1)
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Erreur écriture Sheets : {exc}")
