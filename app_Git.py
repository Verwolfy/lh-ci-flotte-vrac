"""
═══════════════════════════════════════════════════════════════════════════════
ÉTAPE 4 — DASHBOARD STREAMLIT (Version Intégrale - Planification & Dispatch)
LH Côte d'Ivoire | Mapping Vrac & Optimisation Flotte
═══════════════════════════════════════════════════════════════════════════════
"""

import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import folium
from streamlit_folium import st_folium
import plotly.express as px
import os
import math

# ── CONFIGURATION DE LA PAGE ─────────────────────────────────────────────────
st.set_page_config(page_title="LH CI - Flotte Vrac & Dispatch", page_icon="📅", layout="wide")

# ── PARAMÈTRES & CHEMINS SECURES ─────────────────────────────────────────────
SHEET_ID = "14ix_pet8yn7b_yG7rfd7GwqcsNFEU4BSCVzWr8FVRBc"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SERVICE_ACCOUNT_FILE = os.path.join(BASE_DIR, "service_account.json")

def fetch_with_auto_head(ws):
    """Télécharge la grille brute et trouve la ligne d'en-tête technique sans planter"""
    try:
        raw_data = ws.get_all_values()
        if not raw_data:
            return pd.DataFrame()

        for i, row in enumerate(raw_data):
            cleaned_row = [str(cell).strip() for cell in row]
            if "client_id" in cleaned_row or "parametre" in cleaned_row or "camion_id" in cleaned_row:
                headers = cleaned_row
                data = raw_data[i + 1:]
                return pd.DataFrame(data, columns=headers)

        return pd.DataFrame()
    except Exception as e:
        st.error(f"Erreur de lecture de l'onglet {ws.title}: {e}")
        return pd.DataFrame()


# ── CHARGEMENT ET TRAITEMENT DES DONNÉES ─────────────────────────────────────
@st.cache_data(ttl=600)
def load_data():
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    
    # --- AUTHENTIFICATION SÉCURISÉE CLOUD & LOCAL ---
    # On essaie d'abord de lire le coffre-fort de Secrets de Streamlit Cloud
    if "gcp_service_account" in st.secrets:
        try:
            creds_dict = dict(st.secrets["gcp_service_account"])
            creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        except Exception as e:
            st.error(f"Erreur lors de la lecture des Secrets Cloud : {e}")
            return pd.DataFrame()
    else:
        # Si on est sur ton PC (pas sur le cloud), on utilise le fichier local
        if os.path.exists(SERVICE_ACCOUNT_FILE):
            creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
        else:
            st.error("Fichier d'authentification 'service_account.json' introuvable en local et aucun Secret détecté sur le Cloud.")
            return pd.DataFrame()
    # ------------------------------------------------
    
    try:
        gc = gspread.authorize(creds)
        wb = gc.open_by_key(SHEET_ID)
    except Exception as e:
        st.error(f"Erreur de connexion à Google Sheets (Vérifiez l'ID ou les permissions de l'e-mail du robot) : {e}")
        return pd.DataFrame()
    
    # Lecture des onglets nécessaires
    df_sites = fetch_with_auto_head(wb.worksheet("CLIENTS_SITES"))
    df_flotte = fetch_with_auto_head(wb.worksheet("FLOTTE_CLIENTS"))
    df_volumes = fetch_with_auto_head(wb.worksheet("VOLUMES_SAP"))
    df_flotte_lh = fetch_with_auto_head(wb.worksheet("FLOTTE_LH"))
    
    try:
        df_params = fetch_with_auto_head(wb.worksheet("PARAMETRES")).set_index("parametre")
        spot_ref = float(df_params.loc["tarif_spot_ref_fcfa_t", "valeur"])
    except:
        spot_ref = 4500.0
        
    try:
        tonnage_std = float(df_params.loc["tonnage_camion_std", "valeur"])
    except:
        tonnage_std = 27.0
        
    try:
        coeff_remplissage = float(df_params.loc["coef_remplissage_min", "valeur"])
    except:
        coeff_remplissage = 0.92

    # Sécurité si un DataFrame vital est vide
    for df_temp, name in [(df_sites, "CLIENTS_SITES"), (df_flotte, "FLOTTE_CLIENTS"), (df_volumes, "VOLUMES_SAP")]:
        if df_temp.empty or "client_id" not in df_temp.columns:
            st.error(f"⚠️ Impossible de trouver la colonne 'client_id' dans l'onglet : {name}.")
            return pd.DataFrame()

    # Nettoyage des chaînes
    for d in [df_sites, df_flotte, df_volumes, df_flotte_lh]:
        if "client_id" in d.columns: 
            d["client_id"] = d["client_id"].astype(str).str.strip()
    
    if "cap_mensuelle_t" in df_flotte.columns:
        df_flotte.rename(columns={"cap_mensuelle_t": "capacite_mensuelle_t"}, inplace=True)
    
    # Conversions numériques
    cap_col = "capacite_mensuelle_t" if "capacite_mensuelle_t" in df_flotte.columns else df_flotte.columns[3]
    df_flotte[cap_col] = pd.to_numeric(df_flotte[cap_col], errors="coerce").fillna(0)
    
    vol_col = "volume_exw_t" if "volume_exw_t" in df_volumes.columns else df_volumes.columns[5]
    df_volumes[vol_col] = pd.to_numeric(df_volumes[vol_col], errors="coerce").fillna(0)
    
    # Agrégations pour le surplus
    agg_flotte = df_flotte.groupby("client_id").agg(capacite_t_mois=(cap_col, "sum")).reset_index()
    agg_volumes = df_volumes.groupby("client_id").agg(besoin_exw_t_mois=(vol_col, "mean")).reset_index()
    
    df_main = df_sites.merge(agg_flotte, on="client_id", how="left").merge(agg_volumes, on="client_id", how="left")
    df_main.fillna({"capacite_t_mois": 0, "besoin_exw_t_mois": 0}, inplace=True)
    df_main["surplus_t_mois"] = (df_main["capacite_t_mois"] - df_main["besoin_exw_t_mois"]).clip(lower=0)
    
    # Calculs Financiers
    df_main["tarif_spot_ref"] = spot_ref
    df_main["tarif_cible_client"] = df_main["tarif_spot_ref"] * 0.85
    df_main["gain_par_tonne"] = df_main["tarif_spot_ref"] - df_main["tarif_cible_client"]
    df_main["economie_potentielle_mensuelle"] = df_main["surplus_t_mois"] * df_main["gain_par_tonne"]
    
    return df_main, df_flotte_lh, spot_ref, tonnage_std, coeff_remplissage

# ── INTERFACE UTILISATEUR ────────────────────────────────────────────────────
st.title("🚛 LH Côte d'Ivoire — Tournées & Planification Flotte Vrac")

if st.sidebar.button("🔄 Rafraîchir les données"):
    st.cache_data.clear()
    st.rerun()

try:
    df, df_lh, spot_ref, tonnage_std, coeff_remplissage = load_data()

    if not df.empty and "client_nom" in df.columns:
        surplus_total = df['surplus_t_mois'].sum()
        economie_totale = df['economie_potentielle_mensuelle'].sum()

        # Section haute : Métriques Générales
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Capacité Totale Flotte Clients", f"{df['capacite_t_mois'].sum():,.0f} T/mois")
        col2.metric("Surplus Total Exploitable", f"{surplus_total:,.0f} T/mois")
        col3.metric("Économie Mensuelle Target", f"{economie_totale:,.0f} FCFA")

        # Compter la flotte opérationnelle dans FLOTTE_LH
        if not df_lh.empty and "status" in df_lh.columns:
            nb_camions_actifs = len(df_lh[df_lh["status"].str.upper() == "ACTIF"])
            col4.metric("Camions Actifs Pool LH", nb_camions_actifs)
        else:
            col4.metric("Camions Actifs Pool LH", "Dispo (Sheet)")

        st.markdown("---")

        # Les Onglets Métiers
        tab1, tab2, tab3, tab4 = st.tabs([
            "🗺️ Carte Interactive",
            "📊 Analyse Surplus de Flotte",
            "💸 Rentabilité Financière",
            "📅 Planification des Tournées (Dispatch)"
        ])

        # ONGLET 1 : CARTE
        with tab1:
            st.subheader("Cartographie des sites clients")
            df_map = df.dropna(subset=['latitude', 'longitude'])
            df_map = df_map[(df_map['latitude'] != '') & (df_map['longitude'] != '')]
            if not df_map.empty:
                m = folium.Map(location=[5.3599, -4.0083], zoom_start=11, tiles="OpenStreetMap")
                folium.Marker([5.3599, -4.0083], popup="Usine LH CI", icon=folium.Icon(color="black", icon="industry", prefix='fa')).add_to(m)
                for idx, row in df_map.iterrows():
                    try:
                        lat, lon = float(row['latitude']), float(row['longitude'])
                        color = "green" if row['surplus_t_mois'] > 0 else "blue"
                        popup_text = f"<b>{row['client_nom']}</b><br>Surplus: {row['surplus_t_mois']} T"
                        folium.Marker([lat, lon], popup=popup_text, icon=folium.Icon(color=color)).add_to(m)
                    except: pass
                st_folium(m, width=1000, height=400)
            else:
                st.warning("Aucune coordonnée GPS trouvée.")

        # ONGLET 2 : SURPLUS
        with tab2:
            st.subheader("Volume de surplus disponible")
            st.dataframe(df[["client_id", "client_nom", "zone", "capacite_t_mois", "besoin_exw_t_mois", "surplus_t_mois"]].sort_values("surplus_t_mois", ascending=False), use_container_width=True)

        # ONGLET 3 : FINANCE
        with tab3:
            st.subheader("Matrice des opportunités de gains")
            df_display_fin = df[["client_id", "client_nom", "zone", "surplus_t_mois", "tarif_cible_client", "economie_potentielle_mensuelle"]].copy()
            df_display_fin.columns = ["Code SAP", "Nom Client", "Zone", "Surplus (T/mois)", "Tarif Cible (FCFA/T)", "Économie Potentielle (FCFA/mois)"]
            st.dataframe(df_display_fin.sort_values("Économie Potentielle (FCFA/mois)", ascending=False), use_container_width=True)

        # 🚀 ONGLET 4 : PLANIFICATION & DISPATCH (NOUVEAU)
        with tab4:
            st.subheader("Moteur d'Optimisation Quotidien")
            st.write("Simulez les demandes de livraison du jour pour générer le plan d'affectation automatique de la flotte.")

            # Formulaire de simulation des commandes du jour
            c1, c2, c3 = st.columns(3)
            vol_zone_a = c1.number_input("Volume total commandé - Zone A (Grand Abidjan) (T)", min_value=0, value=270, step=27)
            vol_zone_b = c2.number_input("Volume total commandé - Zone B (Périphérie) (T)", min_value=0, value=108, step=27)
            vol_zone_c = c3.number_input("Volume total commandé - Zone C (Intérieur) (T)", min_value=0, value=54, step=27)

            # Calcul du nombre de camions complets nécessaires (Règle >92% de charge std)
            cmd_t_totale = vol_zone_a + vol_zone_b + vol_zone_c
            camions_requis_a = math.ceil(vol_zone_a / tonnage_std)
            camions_requis_b = math.ceil(vol_zone_b / tonnage_std)
            camions_requis_c = math.ceil(vol_zone_c / tonnage_std)
            total_camions_requis = camions_requis_a + camions_requis_b + camions_requis_c

            # Simulation de la capacité du Pool LH à la journée
            # (Règle : Zone A = 3 rotations/j, Zone B = 2 rotations/j, Zone C = 1 rotation/j)
            st.markdown("### 📋 Plan de Chargement Généré")

            # Tableau récapitulatif
            summary_data = {
                "Zone": ["Zone A (Abidjan)", "Zone B (Périphérie)", "Zone C (Intérieur)", "TOTAL"],
                "Volume Commandé (T)": [vol_zone_a, vol_zone_b, vol_zone_c, cmd_t_totale],
                "Voyages Complets Requis": [camions_requis_a, camions_requis_b, camions_requis_c, total_camions_requis],
                "Rotations Moyennes / Camion": [3, 2, 1, "-"],
                "Nombre Camions Physiques Requis": [math.ceil(camions_requis_a/3), math.ceil(camions_requis_b/2), math.ceil(camions_requis_c/1), ""]
            }
            summary_df = pd.DataFrame(summary_data)

            # Calcul dynamique du nombre total de camions physiques nécessaires
            nb_physique_total = math.ceil(camions_requis_a/3) + math.ceil(camions_requis_b/2) + math.ceil(camions_requis_c/1)
            summary_df.iloc[3, 4] = nb_physique_total
            st.table(summary_df)

            # Alerte de surcharge de zone
            st.markdown("### ⚠️ Alertes de Capacité")
            limite_chargement_usine_jour = 40  # Capacité max théorique des silos de chargement de la cimenterie
            if total_camions_requis > limite_chargement_usine_jour:
                st.error(f"🚨 **Goulot d'étranglement Usine :** Le plan requiert {total_camions_requis} chargements. La capacité maximale pneumatique de la cimenterie est de {limite_chargement_usine_jour} rotations/jour. Risque d'attente prolongée sur la piste.")
            else:
                st.success(f"🟢 **Fluidité Usine OK :** {total_camions_requis} chargements planifiés. Volume absorbable par les infrastructures de l'usine.")

            # Suggestion d'activation du surplus contractualisé client
            st.markdown("### 🤝 Recommandation d'Activation Flotte Client")
            df_prospects = df[df["surplus_t_mois"] > 100].sort_values("surplus_t_mois", ascending=False)

            if nb_physique_total > 10 and not df_prospects.empty:
                meilleur_choix = df_prospects.iloc[0]["client_nom"]
                st.info(f"💡 **Optimisation Transport :** Pour couvrir vos besoins du jour sans faire appel au Spot cher, activez en priorité les camions en surplus de **{meilleur_choix}** (Tarif négocié à -15% disponible).")
            else:
                st.write("Le pool de camions permanents est suffisant pour couvrir la demande actuelle.")

except Exception as e:
    st.error(f"Erreur d'exécution de l'application : {e}")
